"""Execute operator commands against the selected Theo data root.

Loads configuration, manages the database lifetime and routes administrative
operations to their owning services. Argument syntax lives in parser.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from theo.application.service import serve
from theo.application.status import status
from theo.backends.factory import backend_for
from theo.backends.policy import Accounts, configuration_files, inspect_configuration
from theo.cli.credentials import telegram_token
from theo.cli.diagnostics import doctor
from theo.config import Settings, load_settings, save_settings
from theo.domain import Denied, digest, uid
from theo.execution.isolation import verify_isolation
from theo.memory.store import Memory
from theo.operations.backups import backup_create, backup_verify, restore_backup
from theo.operations.export import export_data
from theo.operations.importer import import_luke
from theo.operations.releases import Releases
from theo.storage import Database
from theo.supervisor import service_definition
from theo.work.jobs import Jobs


async def execute(args: argparse.Namespace) -> Any:
    root = args.data_root.resolve()
    from theo.observability import telemetry

    telemetry.configure(root, "theo" if args.command == "serve" else "theo-cli")
    if args.command == "init":
        if (root / "config.json").exists():
            return {"initialized": True, "existing_configuration_preserved": True}
        settings = Settings(
            owner_id=args.owner,
            timezone=args.timezone,
            telegram_owner_id=args.telegram_owner_id,
            telegram_chat_id=args.telegram_chat_id,
            worker_home=args.worker_home,
            encrypted_storage_verified=args.encrypted_storage,
        )
        save_settings(root, settings)
    else:
        if not (root / "config.json").exists():
            raise Denied("Run theo init for this data root first")
        settings = load_settings(root)
    db = Database(root)
    try:
        await db.initialize(settings.owner_id, settings.timezone)
        owner = settings.owner_id
        if args.command == "init":
            return {
                "initialized": True,
                "name": settings.name,
                "autonomy": "paused",
                "accounts": "none",
            }
        if args.command == "configure":
            new = Settings.model_validate_json(args.file.read_text())
            if new.owner_id != owner:
                raise Denied("Owner binding cannot be changed by configuration replacement")
            save_settings(root, new)
            await db.execute("UPDATE backend_accounts SET status='requires_reverification'")
            return {"configured": True, "account_reverification_required": True}
        if args.command == "telegram":
            from theo.channels.telegram.diagnostics import diagnostics

            if args.operation == "retry-event":
                changed = await db.execute(
                    "UPDATE telegram_events SET status='pending',attempts=0,available_at=?,error=NULL WHERE owner_id=? AND bot_id=? AND update_id=? AND status='failed'",
                    (db.clock(), owner, args.bot_id, args.update_id),
                )
                return {"queued": bool(changed)}
            return await diagnostics(
                db,
                settings,
                telegram_token(settings.telegram_keychain_service)
                if args.operation == "doctor"
                else None,
            )
        if args.command == "doctor":
            return await doctor(db, settings)
        if args.command == "status":
            return await status(db, settings)
        if args.command == "serve":
            if bool(settings.telegram_owner_id) != bool(settings.telegram_chat_id):
                raise Denied("Telegram requires both an exact owner ID and chat ID")
            await serve(db, settings, telegram_token(settings.telegram_keychain_service))
            return None
        if args.command == "chat":
            if args.text is None:
                from theo.channels.terminal.interface import interactive

                await interactive(db, settings, args.session, args.backend, args.model, args.attach)
                return None
            conversation = await db.conversation(owner, "local", owner)
            if args.backend:
                if not args.model:
                    raise ValueError("Selecting a backend requires an explicit model")
                await db.execute(
                    "UPDATE conversations SET backend=?,model=? WHERE id=?",
                    (args.backend, args.model, conversation),
                )
            from theo.channels.terminal.attachments import attachment_parts

            parts = await attachment_parts(db, settings, args.attach)
            job = await Jobs(db, owner).ingest(
                conversation, "local", uid(), {"source": "operator_cli"}, args.text, parts
            )
            return {
                "job_id": job,
                "status": "queued",
                "instruction": "theo serve processes this durable message",
            }
        if args.command in ("accounts", "models"):
            if args.command == "models":
                return await db.read(
                    "SELECT backend,models,status,verified_at FROM backend_accounts WHERE owner_id=?",
                    (owner,),
                )
            if args.operation == "list":
                return await Accounts(db, owner).usage()
            if args.operation == "quota":
                changed = await db.execute(
                    "UPDATE backend_accounts SET quota_status='available',reset_at=NULL WHERE owner_id=? AND pool_id IN (SELECT pool_id FROM backend_accounts WHERE owner_id=? AND backend=?)",
                    (owner, owner, args.backend),
                )
                return {
                    "updated_accounts_in_shared_pools": changed,
                    "basis": "explicit_operator_confirmation_of_renewed_allowance",
                }
            evidence = json.loads(args.evidence.read_text())
            backend = backend_for(args.backend, db=db, settings=settings)
            version = await backend.version()
            if settings.worker_home is None:
                raise Denied("Configure the native runner home first")
            config_hash = inspect_configuration(
                configuration_files(settings.worker_home, args.backend)
            )
            expected_fingerprint = digest(
                {"backend": args.backend, "version": version, "transport": "theo-v1"}
            )
            if (
                evidence.get("runtime_version") != version
                or evidence.get("fingerprint") != expected_fingerprint
                or evidence.get("config_hash") != config_hash
            ):
                return {
                    "verified": False,
                    "reason": "Evidence must match this installed runtime and effective configuration",
                    "runtime_version": version,
                    "fingerprint": expected_fingerprint,
                    "config_hash": config_hash,
                }
            account_id = await Accounts(db, owner).register(args.backend, evidence)
            return {
                "account_id": account_id,
                "verified": True,
                "method": evidence["verification_method"],
            }
        if args.command in ("jobs", "runs", "actions"):
            table = args.command
            if args.operation == "list":
                return await db.read(
                    f"SELECT * FROM {table} WHERE owner_id=? ORDER BY rowid DESC LIMIT 30", (owner,)
                )
            if args.operation == "inspect":
                row = await db.one(
                    f"SELECT * FROM {table} WHERE id=? AND owner_id=?", (args.id, owner)
                )
                if table == "runs" and row and row.get("context_id"):
                    row["context"] = await db.one(
                        "SELECT * FROM context_snapshots WHERE id=? AND owner_id=?",
                        (row["context_id"], owner),
                    )
                if table == "actions" and row:
                    row["chunks"] = await db.read(
                        "SELECT * FROM outbox WHERE action_id=? AND owner_id=? ORDER BY ordinal",
                        (args.id, owner),
                    )
                return row
            if table == "jobs" and args.operation == "cancel":
                return {"cancelled": await Jobs(db, owner).cancel(args.id)}
            if table == "jobs" and args.operation == "retry":
                old = await db.one("SELECT * FROM jobs WHERE id=? AND owner_id=?", (args.id, owner))
                if old and old["status"] == "failed":
                    dangerous = await db.one(
                        "SELECT id FROM actions WHERE job_id=? AND status IN ('uncertain','executing')",
                        (args.id,),
                    )
                    if dangerous:
                        raise Denied("Reconcile uncertain actions before retrying this job")
                    new_id = await Jobs(db, owner).enqueue(
                        old["conversation_id"],
                        old["kind"],
                        json.loads(old["payload"]),
                        "retry:" + old["id"] + ":" + uid(),
                        lane=old["lane"],
                        parent=old["parent_id"],
                        deadline=db.clock() + settings.deadline_seconds,
                    )
                    return {"queued": True, "job_id": new_id, "previous_job_id": args.id}
                changed = await db.execute(
                    "UPDATE jobs SET status='queued',deadline=?,available_at=? WHERE id=? AND owner_id=? AND status IN ('waiting_for_auth','waiting_for_quota','interrupted') AND NOT EXISTS(SELECT 1 FROM actions WHERE job_id=? AND status IN ('uncertain','executing'))",
                    (db.clock() + settings.deadline_seconds, db.clock(), args.id, owner, args.id),
                )
                return {"queued": bool(changed)}
            if table == "actions" and args.operation in ("approve", "reject"):
                from theo.delivery.ledger import Delivery

                row = await db.one(
                    "SELECT p.id,a.conversation_id,a.request_hash FROM approvals p JOIN actions a ON a.id=p.action_id WHERE a.id=? AND a.owner_id=? AND p.decision='pending'",
                    (args.id, owner),
                )
                if row is None or row["request_hash"] != args.request_hash:
                    raise Denied(
                        "Inspect the exact pending action and use its current request_hash"
                    )
                await Delivery(db, settings).decide(
                    row["id"], row["conversation_id"], args.operation == "approve"
                )
                return {"decision": args.operation, "action_id": args.id}
            if table == "actions" and args.operation == "reconcile":
                from theo.delivery.ledger import Delivery

                await Delivery(db, settings).reconcile(
                    args.id,
                    receipt=json.loads(args.receipt.read_text()) if args.receipt else None,
                    confirmed_no_effect=args.confirmed_no_effect,
                    delivery_id=args.delivery_id,
                )
                return {"reconciled": True}
        if args.command == "memory":
            memory = Memory(db, owner)
            if args.operation == "list":
                return await db.read(
                    "SELECT id,kind,revision,status FROM memory_records WHERE owner_id=?", (owner,)
                )
            if args.operation == "search":
                return await memory.search(args.query)
            if args.operation == "remember":
                return {
                    "id": await memory.remember(args.body, source="owner:cli", provenance="owner")
                }
            if args.operation == "show":
                return await memory.show(args.id)
            if args.operation == "history":
                return await memory.history(args.id)
            if args.operation == "archive":
                await memory.archive(args.id)
                return {"archived": True}
            if args.operation == "erase":
                await memory.erase(args.id)
                return {
                    "erased_from_active_memory": True,
                    "note": "Historical backups and independently retained provider/user-message records require separate erasure.",
                }
            if args.operation == "restore":
                return {"revision": await memory.restore(args.id, args.revision)}
            if args.operation == "review":
                return await memory.review(args.id, args.accept)
            if args.operation == "export":
                return {"path": str(await export_data(db, args.output, args.format))}
            if args.operation == "edit":
                row = await memory.show(args.id)
                expected = (
                    args.expected_revision
                    if args.expected_revision is not None
                    else row["revision"]
                )
                if args.file:
                    body = args.file.read_text()
                else:
                    import shlex
                    import tempfile

                    with tempfile.TemporaryDirectory(prefix="theo-edit-") as directory:
                        path = Path(directory) / "memory.txt"
                        path.write_text(row["body"])
                        subprocess.run(
                            [*shlex.split(os.environ.get("EDITOR", "vi")), str(path)], check=True
                        )
                        body = path.read_text()
                return {"revision": await memory.edit(args.id, expected, body, source="owner:cli")}
        if args.command == "import":
            return await import_luke(db, owner, args.source, args.apply)
        if args.command == "backup":
            return (
                {"snapshot": str(await backup_create(db, settings))}
                if args.operation == "create"
                else await backup_verify(args.snapshot)
            )
        if args.command == "restore":
            return await restore_backup(args.source, args.target, settings)
        if args.command == "recovery":
            from theo.operations.releases import release_recovery

            if args.operation == "release":
                return await release_recovery(db, settings, args.snapshot_time)
            return {
                "quarantined": await db.control(owner, "quarantined"),
                "snapshot_time": await db.control(owner, "recovery_since"),
                "uncertain_jobs": await db.read(
                    "SELECT id,status FROM jobs WHERE owner_id=? AND status='uncertain'", (owner,)
                ),
                "uncertain_actions": await db.read(
                    "SELECT id,status FROM actions WHERE owner_id=? AND status='uncertain'",
                    (owner,),
                ),
            }
        if args.command == "isolation":
            report = await verify_isolation(settings, root)
            save_settings(
                root, settings.model_copy(update={"isolation_verified": report["verified"]})
            )
            return report
        if args.command == "service":
            if args.operation == "install":
                target = args.output or root / "local.theo.supervisor.plist"
                target.write_bytes(service_definition(root, Path(sys.executable)))
                return {
                    "service_definition": str(target),
                    "loaded": False,
                    "next": "Review the definition, then use launchctl bootstrap in your user session for deliberate activation.",
                }
            pause = root / "maintenance.pause"
            if args.operation == "pause":
                pause.touch(mode=0o600)
            else:
                pause.unlink(missing_ok=True)
            return {"service": "paused" if args.operation == "pause" else "resumed"}
        if args.command == "release-stage":
            return await Releases(db, settings).stage(args.source)
        if args.command in ("upgrade", "rollback"):
            return await Releases(db, settings).switch(args.release)
        if args.command == "assets":
            from theo.memory.embeddings import Embeddings

            if args.operation == "install-embeddings":
                return await Embeddings(db, owner).install()
            if args.operation == "repair-embeddings":
                return {"processed": await Embeddings(db, owner).repair_one()}
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium"
            )
            return {"exit_code": await process.wait()}
        if args.command == "goals":
            return await db.read("SELECT * FROM goals WHERE owner_id=?", (owner,))
        if args.command == "qualification":
            from theo.operations.qualification import qualification_status, record_qualification

            if args.operation == "status":
                return await qualification_status(db, settings)
            return {
                "qualification_id": await record_qualification(
                    db, settings, json.loads(args.file.read_text())
                )
            }
        if args.command == "facts":
            memory = Memory(db, owner)
            if args.operation == "list":
                return await memory.current_facts()
            return {
                "id": await memory.set_fact(
                    args.subject,
                    args.predicate,
                    args.value,
                    "owner:cli",
                    expected=args.expected_revision,
                    valid_to=args.valid_until,
                )
            }
        if args.command == "skills":
            from theo.work.improvement import Improvement

            improvement = Improvement(db, owner)
            if args.operation == "list":
                return await db.read(
                    "SELECT * FROM skills WHERE owner_id=? ORDER BY name,version", (owner,)
                )
            if args.operation == "evaluate":
                return await improvement.evaluate_skill(args.id, json.loads(args.cases.read_text()))
            if args.operation == "activate":
                await improvement.activate_skill(args.id)
            else:
                await improvement.rollback_skill(args.id)
            return {"skill_id": args.id, "operation": args.operation}
        if args.command == "evaluate":
            tests = Path(__file__).resolve().parents[2] / "tests"
            if not tests.exists():
                raise Denied(
                    "Offline evaluation requires the source checkout and locked dev environment"
                )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                str(tests),
                env={**os.environ, "THEO_TEST_OFFLINE": "1"},
            )
            return {"exit_code": await process.wait(), "live_model_calls": 0}
        raise ValueError("Unsupported command")
    finally:
        await db.close()
