"""Operator entry point. Administrative controls are deliberately absent from MCP."""

import argparse
import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from theo import __version__
from theo.backends import backend_for
from theo.backends.policy import BACKENDS, Accounts, configuration_files, inspect_configuration
from theo.config import Settings, default_root, load_settings, save_settings
from theo.domain import Denied, Json, TheoError, digest, uid
from theo.importer import import_luke
from theo.isolation import verify_isolation
from theo.jobs import Jobs
from theo.memory import Memory
from theo.operations import Releases, backup_create, backup_verify, export_data, restore_backup
from theo.runtime import serve, status
from theo.storage import Database
from theo.supervisor import service_definition


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="theo", description="Theo — durable personal assistance, included subscriptions only"
    )
    cli.add_argument("--data-root", type=Path, default=default_root())
    cli.add_argument("--version", action="version", version=__version__)
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--owner", default="owner")
    init.add_argument("--timezone", default="Europe/Dublin")
    init.add_argument("--telegram-owner-id", type=int)
    init.add_argument("--telegram-chat-id", type=int)
    init.add_argument("--worker-home", type=Path)
    init.add_argument("--encrypted-storage", action="store_true")
    configure = sub.add_parser("configure")
    configure.add_argument("--file", type=Path, required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("status")
    chat = sub.add_parser("chat")
    chat.add_argument("text")
    chat.add_argument("--backend", choices=BACKENDS)
    chat.add_argument("--model")
    accounts = sub.add_parser("accounts").add_subparsers(dest="operation", required=True)
    accounts.add_parser("list")
    quota = accounts.add_parser("quota")
    quota.add_argument("backend", choices=BACKENDS)
    quota.add_argument("--available", action="store_true", required=True)
    verify = accounts.add_parser("verify")
    verify.add_argument("backend", choices=BACKENDS)
    verify.add_argument("--evidence", type=Path, required=True)
    models = sub.add_parser("models").add_subparsers(dest="operation", required=True)
    models.add_parser("list")
    for noun in ("jobs", "runs", "actions"):
        commands = sub.add_parser(noun).add_subparsers(dest="operation", required=True)
        commands.add_parser("list")
        inspect = commands.add_parser("inspect")
        inspect.add_argument("id")
        if noun == "jobs":
            cancel = commands.add_parser("cancel")
            cancel.add_argument("id")
            retry = commands.add_parser("retry")
            retry.add_argument("id")
        if noun == "actions":
            for decision in ("approve", "reject"):
                approval = commands.add_parser(decision)
                approval.add_argument("id")
                approval.add_argument("--request-hash", required=True)
            reconcile = commands.add_parser("reconcile")
            reconcile.add_argument("id")
            reconcile.add_argument("--receipt", type=Path)
            reconcile.add_argument("--confirmed-no-effect", action="store_true")
    memory = sub.add_parser("memory").add_subparsers(dest="operation", required=True)
    memory.add_parser("list")
    search = memory.add_parser("search")
    search.add_argument("query")
    for verb in ("show", "history", "archive", "erase"):
        command = memory.add_parser(verb)
        command.add_argument("id")
    remember = memory.add_parser("remember")
    remember.add_argument("body")
    edit = memory.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("--file", type=Path)
    edit.add_argument("--expected-revision", type=int)
    restore = memory.add_parser("restore")
    restore.add_argument("id")
    restore.add_argument("--revision", type=int)
    export = memory.add_parser("export")
    export.add_argument("--format", choices=("jsonl", "markdown"), default="jsonl")
    export.add_argument("--output", type=Path, required=True)
    review = memory.add_parser("review")
    review.add_argument("id")
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    imports = sub.add_parser("import").add_subparsers(dest="operation", required=True)
    luke = imports.add_parser("luke")
    luke.add_argument("--source", type=Path, required=True)
    mode = luke.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    backup = sub.add_parser("backup").add_subparsers(dest="operation", required=True)
    backup.add_parser("create")
    verify_backup = backup.add_parser("verify")
    verify_backup.add_argument("snapshot", type=Path)
    restore_app = sub.add_parser("restore")
    restore_app.add_argument("--source", type=Path, required=True)
    restore_app.add_argument("--target", type=Path, required=True)
    recovery = sub.add_parser("recovery").add_subparsers(dest="operation", required=True)
    recovery.add_parser("inspect")
    release_recovery = recovery.add_parser("release")
    release_recovery.add_argument("--snapshot-time", type=float, required=True)
    services = sub.add_parser("service").add_subparsers(dest="operation", required=True)
    install = services.add_parser("install")
    install.add_argument("--output", type=Path)
    for command in ("pause", "resume"):
        services.add_parser(command)
    for command in ("upgrade", "rollback"):
        release = sub.add_parser(command)
        release.add_argument("--release", required=True)
    stage = sub.add_parser("release-stage")
    stage.add_argument("source", type=Path)
    isolation = sub.add_parser("isolation").add_subparsers(dest="operation", required=True)
    isolation.add_parser("verify")
    asset = sub.add_parser("assets").add_subparsers(dest="operation", required=True)
    asset.add_parser("install-embeddings")
    asset.add_parser("repair-embeddings")
    asset.add_parser("install-browser")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--suite", default="acceptance", choices=("acceptance",))
    evaluate.add_argument("--offline", action="store_true", required=True)
    sub.add_parser("goals")
    qualification = sub.add_parser("qualification").add_subparsers(dest="operation", required=True)
    qualification.add_parser("status")
    qualification_record = qualification.add_parser("record")
    qualification_record.add_argument("--file", type=Path, required=True)
    skills = sub.add_parser("skills").add_subparsers(dest="operation", required=True)
    skills.add_parser("list")
    for operation in ("evaluate", "activate", "rollback"):
        skill = skills.add_parser(operation)
        skill.add_argument("id")
        if operation == "evaluate":
            skill.add_argument("--cases", type=Path, required=True)
    facts = sub.add_parser("facts").add_subparsers(dest="operation", required=True)
    facts.add_parser("list")
    fact = facts.add_parser("set")
    for field in ("subject", "predicate", "value"):
        fact.add_argument(field)
    fact.add_argument("--expected-revision", type=int, default=0)
    fact.add_argument("--valid-until", type=float)
    return cli


async def doctor(db: Database, settings: Settings) -> Json:
    from theo.tools import REGISTRY

    checks: Json = {
        "application": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "disk_free_bytes": shutil.disk_usage(db.root).free,
        "schema": await db.read("SELECT * FROM schema_migrations"),
        "tools": len(REGISTRY),
        "isolation_verified": settings.isolation_verified,
        "encrypted_storage_verified": settings.encrypted_storage_verified,
    }
    checks["database_integrity"] = await db.read("PRAGMA integrity_check")
    checks["foreign_key_violations"] = await db.read("PRAGMA foreign_key_check")
    checks["backends"] = {
        name: {"installed": shutil.which("agent" if name == "cursor" else name) is not None}
        for name in BACKENDS
    }
    checks["accounts"] = await Accounts(db, settings.owner_id).usage()
    checks["status"] = await status(db, settings)
    from theo.qualification import qualification_status

    checks["qualification"] = await qualification_status(db, settings)
    checks["production_qualified"] = checks["qualification"]["production_qualified"]
    checks["assets"] = {
        "embeddings": (db.root / "models/embeddings/manifest.json").exists(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }
    return checks


def telegram_token() -> str | None:
    if os.environ.get("THEO_TEST_OFFLINE") == "1":
        return None
    token = os.environ.get("THEO_TELEGRAM_TOKEN")
    if token:
        return token
    if sys.platform == "darwin":
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "theo.telegram", "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None


async def execute(args: argparse.Namespace) -> Any:
    root = args.data_root.resolve()
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
        if args.command == "doctor":
            return await doctor(db, settings)
        if args.command == "status":
            return await status(db, settings)
        if args.command == "serve":
            if bool(settings.telegram_owner_id) != bool(settings.telegram_chat_id):
                raise Denied("Telegram requires both an exact owner ID and chat ID")
            await serve(db, settings, telegram_token())
            return None
        if args.command == "chat":
            conversation = await db.conversation(owner, "local", owner)
            if args.backend:
                if not args.model:
                    raise ValueError("Selecting a backend requires an explicit model")
                await db.execute(
                    "UPDATE conversations SET backend=?,model=? WHERE id=?",
                    (args.backend, args.model, conversation),
                )
            job = await Jobs(db, owner).ingest(
                conversation, "local", uid(), {"source": "operator_cli"}, args.text
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
                        deadline=db.clock() + settings.deadline_seconds,
                    )
                    return {"queued": True, "job_id": new_id, "previous_job_id": args.id}
                changed = await db.execute(
                    "UPDATE jobs SET status='queued',deadline=?,available_at=? WHERE id=? AND owner_id=? AND status IN ('waiting_for_auth','waiting_for_quota','interrupted') AND NOT EXISTS(SELECT 1 FROM actions WHERE job_id=? AND status IN ('uncertain','executing'))",
                    (db.clock() + settings.deadline_seconds, db.clock(), args.id, owner, args.id),
                )
                return {"queued": bool(changed)}
            if table == "actions" and args.operation in ("approve", "reject"):
                from theo.delivery import Delivery

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
                from theo.delivery import Delivery

                await Delivery(db, settings).reconcile(
                    args.id,
                    receipt=json.loads(args.receipt.read_text()) if args.receipt else None,
                    confirmed_no_effect=args.confirmed_no_effect,
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
            from theo.operations import release_recovery

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
            from theo.embeddings import Embeddings

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
            from theo.qualification import qualification_status, record_qualification

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
            from theo.improvement import Improvement

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


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(execute(args))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (TheoError, ValueError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
