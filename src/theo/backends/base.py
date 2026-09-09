"""Shared native-worker lifecycle, account eligibility and event streaming.

Adapters implement execute(); this module bounds the event stream, reports metrics
and process identity, and emits one terminal outcome for each attempt.
"""

import asyncio
import base64
import contextlib
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable

from theo.backends.policy import (
    Accounts,
    configuration_files,
    inspect_configuration,
    worker_environment,
)
from theo.backends.process import stop_process
from theo.config import Settings
from theo.domain import (
    AuthWait,
    Denied,
    ExecutionEvent,
    ExecutionOutcome,
    ExecutionRequest,
    Json,
    Outcome,
    ProtocolError,
    QuotaWait,
    digest,
)
from theo.observability import telemetry
from theo.storage import Database

type Emitter = Callable[[str, Json], Awaitable[None]]

TOOL_CONTRACT = (
    "You are Theo's reasoning worker. The supplied canonical context and Theo MCP tool "
    "results are your durable state. Execute requests to remember or recall information "
    "with Theo's remember and recall tools; workspace Markdown files and native auto-memory "
    "are not Theo memory. Use file_write for requested artifacts, not substitute memory. "
    "Use Theo MCP tools for goals, scheduling, delegation and all other effects. Native "
    "subagents, goals and automations cannot fulfill Theo's durable obligations. Confirm "
    "a change only after its corresponding tool succeeds; describe pending review or queued "
    "work accurately without claiming it is already applied or completed. Follow the owner's "
    "requested response format and length. For JSON responses, put explanations inside the "
    "requested object, include only the requested keys, and use no prose outside it. "
    "A newer canonical fact revision supersedes older context; it does not prove an earlier "
    "answer lacked evidence or was wrong at the time. Distinguish changed facts from errors. "
    "Preserve the supplied units; do not invent "
    "a currency or other factual detail that the evidence does not specify. For an uncertain "
    "delivery, require evidence for that exact action: a receipt or confirmed no-effect. "
    "Without that evidence, keep it uncertain and do not recommend resending. Online presence, "
    "network health, earlier successful messages or an apology cannot resolve uncertainty."
)


def classify_error(value: str) -> Outcome:
    lowered = value.lower()
    if any(
        word in lowered
        for word in ("rate_limit", "rate limit", "usage_limit", "quota", "limit reached", "429")
    ):
        return Outcome.QUOTA
    if any(word in lowered for word in ("auth", "login", "sign in", "401", "403")):
        return Outcome.AUTH
    return Outcome.FAILED


class NativeBackend:
    name = ""
    binary = ""

    def __init__(self, db: Database, settings: Settings, binary: str | None = None):
        self.db, self.settings = db, settings
        self.binary = binary or self.binary
        self.process: asyncio.subprocess.Process | None = None

    async def version(self) -> str:
        executable = shutil.which(self.binary)
        if not executable:
            raise AuthWait(f"Native {self.name} runtime is not installed")
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AuthWait("Runtime version probe timed out") from None
        if process.returncode or len(raw) > 4096:
            raise ProtocolError("Runtime version probe failed")
        return raw.decode(errors="replace").strip()

    async def preparation(self, request: ExecutionRequest) -> tuple[dict[str, str], Json]:
        if os.environ.get("THEO_TEST_OFFLINE") == "1":
            raise Denied("Live native execution is disabled in offline tests")
        home = self.settings.worker_home
        if home is None:
            raise AuthWait("Configure a native runner home and sign in with its official CLI")
        env = worker_environment(home, runner_uid=self.settings.runner_uid)
        version = await self.version()
        configuration = inspect_configuration(configuration_files(home, self.name))
        fingerprint = digest({"backend": self.name, "version": version, "transport": "theo-v1"})
        account = await Accounts(self.db, request.owner_id).eligible(
            self.name, request.model, fingerprint, configuration
        )
        return env, account

    async def images(self, request: ExecutionRequest) -> list[Json]:
        import io

        from PIL import Image

        from theo.content.artifacts import Artifacts

        images: list[Json] = []
        artifact_ids = [
            part.artifact_id
            for part in request.parts
            if part.kind == "photo" and part.artifact_id and part.metadata.get("state") != "failed"
        ]
        artifact_ids.extend(
            str(value)
            for part in request.parts
            for value in part.metadata.get("derived_photos", [])
        )
        for artifact_id in artifact_ids[:8]:
            _, raw = await Artifacts(self.db, self.settings).content(artifact_id)

            def normalize(raw: bytes = raw) -> bytes:
                with Image.open(io.BytesIO(raw)) as picture:
                    picture.thumbnail((1600, 1600))
                    output = io.BytesIO()
                    picture.convert("RGB").save(output, format="JPEG", quality=80)
                    return output.getvalue()

            image = await asyncio.to_thread(normalize)
            images.append(
                {
                    "mime": "image/jpeg",
                    "data": base64.b64encode(image).decode(),
                    "artifact_id": artifact_id,
                }
            )
        return images

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        raise NotImplementedError

    async def events(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        queue: asyncio.Queue[ExecutionEvent | None] = asyncio.Queue(maxsize=512)
        sequence = 0
        started = asyncio.get_running_loop().time()
        first_output = False

        async def emit(kind: str, payload: Json) -> None:
            nonlocal sequence, first_output
            if kind == "text_delta" and not first_output:
                first_output = True
                telemetry.measure(
                    "theo_ai_first_output_duration",
                    asyncio.get_running_loop().time() - started,
                    histogram=True,
                    backend=self.name,
                )
            if kind == "terminal":
                telemetry.mark_outcome(str(payload.get("status", "unknown")))
                telemetry.measure(
                    "theo_ai_runs", backend=self.name, outcome=str(payload.get("status", "unknown"))
                )
                telemetry.event(
                    "ai.result",
                    backend=self.name,
                    outcome=payload.get("status"),
                    run_id=request.run_id,
                )
                for field, token_type in (("input_tokens", "input"), ("output_tokens", "output")):
                    value = payload.get(field)
                    if isinstance(value, int) and value >= 0:
                        telemetry.measure(
                            "theo_ai_tokens", value, backend=self.name, token_type=token_type
                        )
            sequence += 1
            await queue.put(
                ExecutionEvent(run_id=request.run_id, sequence=sequence, kind=kind, payload=payload)
            )

        async def run() -> None:
            with telemetry.operation(
                "ai.run", backend=self.name, model=request.model, run_id=request.run_id
            ):
                await execute_run()

        async def execute_run() -> None:
            if telemetry.carrier():
                await self.db.execute(
                    "INSERT OR REPLACE INTO telemetry_links VALUES(?,?,?,?)",
                    ("run", request.run_id, telemetry.carrier(), self.db.clock()),
                )
            try:
                await emit("started", {"backend": self.name})
                async with asyncio.timeout(max(0.01, request.deadline - self.db.clock())):
                    outcome = await self.execute(request, emit)
            except QuotaWait:
                outcome = ExecutionOutcome(
                    status=Outcome.QUOTA, error="Included allowance unavailable"
                )
            except AuthWait as exc:
                outcome = ExecutionOutcome(status=Outcome.AUTH, error=str(exc))
            except asyncio.CancelledError:
                await self.cancel()
                raise
            except TimeoutError:
                await self.cancel()
                outcome = ExecutionOutcome(status=Outcome.INTERRUPTED, error="Run deadline reached")
            except (Denied, ProtocolError, OSError) as exc:
                outcome = ExecutionOutcome(status=Outcome.FAILED, error=str(exc))
            except Exception as exc:
                outcome = ExecutionOutcome(
                    status=Outcome.FAILED, error=f"Native adapter failure: {type(exc).__name__}"
                )
            await emit("terminal", outcome.model_dump(mode="json"))
            await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self.cancel()

    async def track(self, request: ExecutionRequest) -> None:
        if self.process and await self.db.one(
            "SELECT id FROM runs WHERE id=? AND owner_id=?", (request.run_id, request.owner_id)
        ):
            from theo.execution.registry import register_worker

            await register_worker(self.db, request.owner_id, request.run_id, self.process.pid)

    async def cancel(self) -> None:
        if self.process:
            await stop_process(self.process)
