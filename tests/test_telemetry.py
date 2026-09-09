import asyncio
import contextvars
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Update
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.channels.telegram.adapter import Telegram
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import ExecutionOutcome, Outcome
from theo.observability import telemetry
from theo.observability.observer import Observer
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.jobs import Jobs


@pytest.fixture
def signals(monkeypatch):
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_metrics", meter)
    monkeypatch.setattr(telemetry, "_instruments", {})
    yield spans, reader
    provider.shutdown()
    meter.shutdown()


async def test_correlation_survives_writer_thread_duplicate_admission_and_restart(
    tmp_path, signals
):
    db = Database(tmp_path)
    await db.initialize()
    conversation = await db.conversation("owner", "local", "test")
    with telemetry.operation("cli.submit") as ingress:
        job = await Jobs(db, "owner").enqueue(
            conversation, "conversation", {"text": "secret"}, "same"
        )
        expected = ingress.get_span_context().trace_id
    with telemetry.operation("retry"):
        duplicate = await Jobs(db, "owner").enqueue(
            conversation, "conversation", {"text": "secret"}, "same"
        )
    assert duplicate == job
    await db.close()
    db = Database(tmp_path)
    link = await db.one(
        "SELECT traceparent FROM telemetry_links WHERE kind='job' AND entity_id=?", (job,)
    )
    with telemetry.operation("job.run", upstream=link["traceparent"]) as span:
        assert span.get_span_context().trace_id == expected
        telemetry.measure("theo_test_duration", 0.1, histogram=True, channel="cli")
    metrics = signals[1].get_metrics_data()
    point = metrics.resource_metrics[0].scope_metrics[0].metrics[-1].data.data_points[0]
    assert point.exemplars[0].trace_id == expected
    payload = await db.one("SELECT payload FROM jobs WHERE id=?", (job,))
    assert json.loads(payload["payload"]) == {"text": "secret"}
    await db.close()


def test_secrets_are_not_recorded_even_on_exceptions(signals):
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    telemetry._logger.addHandler(handler)
    telemetry._logger.setLevel(logging.INFO)
    try:
        with (
            pytest.raises(ValueError),
            telemetry.operation("tool.call", tool="file_read", prompt="secret-content"),
        ):
            telemetry.event("tool.started", token="secret-token", body="secret-body")
            raise ValueError("secret-exception")
    finally:
        telemetry._logger.removeHandler(handler)
    for record in records:
        assert "secret-" not in telemetry.JsonFormatter().format(record)
    span = signals[0].get_finished_spans()[0]
    assert span.status.description == "ValueError"
    assert not span.events
    assert "prompt" not in span.attributes


async def test_observer_does_not_migrate_or_create_a_database(tmp_path):
    observer = Observer(tmp_path)
    await asyncio.to_thread(observer.snapshot)
    assert not (tmp_path / "theo.sqlite3").exists()
    assert (
        observer.registry.get_sample_value("theo_database_readable", {"environment": "local"}) == 0
    )
    db = Database(tmp_path)
    await db.initialize()
    await db.execute(
        "INSERT INTO lifecycle_intervals VALUES(?,?,?,?,?,?)",
        ("test", "owner", db.clock(), None, db.clock(), 0),
    )
    await asyncio.to_thread(observer.snapshot)
    assert (
        observer.registry.get_sample_value("theo_database_readable", {"environment": "local"}) == 1
    )
    assert observer.registry.get_sample_value("theo_core_ready", {"environment": "local"}) == 1
    assert (
        observer.registry.get_sample_value(
            "theo_observability_budget_verified", {"environment": "local"}
        )
        == 0
    )
    await db.close()


def test_grafana_exemplar_and_log_links_target_the_same_trace_backend():
    root = Path(__file__).resolve().parents[1] / "observability/grafana"
    source = (root / "provisioning/datasources/sources.yaml").read_text()
    assert "name: trace_id" in source and "matcherRegex: sampled_trace_id" in source
    assert source.count("datasourceUid: tempo") == 2
    assert "customQuery: true" in source and '| trace_id="$${__trace.traceId}"' in source
    dashboards = [json.loads(p.read_text()) for p in (root / "dashboards").glob("*.json")]
    assert len(dashboards) == 6
    for dashboard in dashboards:
        assert all(
            link.get("includeVars") for link in dashboard["links"] if link["url"].startswith("/d/")
        )
        for panel in dashboard["panels"]:
            if panel["type"] == "timeseries" and panel["datasource"]["uid"] == "prometheus":
                assert all(target["exemplar"] for target in panel["targets"])
            if panel.get("datasource", {}).get("uid") == "loki":
                assert all("$__rate_interval" not in target["expr"] for target in panel["targets"])


@pytest.mark.parametrize("content", ["not-json", "[]", '{"enabled":true,"sample_ratio":2}'])
def test_invalid_telemetry_settings_do_not_prevent_native_startup(tmp_path, content):
    (tmp_path / "telemetry.json").write_text(content)
    telemetry.configure(tmp_path)
    assert not telemetry.enabled()


def test_explicit_disable_overrides_root_configuration(tmp_path, monkeypatch):
    (tmp_path / "telemetry.json").write_text('{"enabled":true}')
    monkeypatch.setenv("THEO_TELEMETRY_ENABLED", "0")
    telemetry.configure(tmp_path)
    assert not telemetry.enabled()


async def test_external_heartbeat_only_pings_when_core_and_stack_are_healthy(tmp_path, monkeypatch):
    calls = []
    failing = False

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(503 if failing and request.url.port == 13100 else 200)

    client = httpx.AsyncClient
    monkeypatch.setenv("THEO_HEARTBEAT_URL", "https://heartbeat.example/private-check")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    observer = Observer(tmp_path)
    await observer.heartbeat()
    assert not calls
    observer.set("theo_core_ready", 1)
    await observer.heartbeat()
    assert calls.count("heartbeat.example") == 1
    failing = True
    await observer.heartbeat()
    assert calls.count("heartbeat.example") == 1
    assert (
        observer.registry.get_sample_value(
            "theo_external_heartbeat_healthy", {"environment": "local"}
        )
        == 0
    )


def telegram_update(number=1, *, album=False):
    message = {
        "message_id": number,
        "date": 1788782400,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False, "first_name": "Fixture"},
        "text": "Trace fixture",
    }
    if album:
        message.pop("text")
        message["media_group_id"] = "fixture-album"
        message["photo"] = [
            {"file_id": str(number), "file_unique_id": str(number), "width": 2, "height": 2}
        ]
    return Update.model_validate({"update_id": number, "message": message})


async def test_telegram_trace_survives_receipt_retry_restart_ai_tool_and_delivery(
    clock, signals, monkeypatch, tmp_path
):
    db = Database(tmp_path / "data", clock)
    await db.initialize()
    settings = Settings(
        encrypted_storage_verified=True,
        telegram_owner_id=123,
        telegram_chat_id=123,
        primary_backend="codex",
        primary_model="fixture-model",
    )
    telegram = Telegram(db, settings, "789:TEST_FIXTURE_TOKEN")
    with telemetry.operation("fixture.receipt") as ingress:
        expected = ingress.get_span_context().trace_id
        await telegram.state.receive(telegram_update())
    original = await db.one("SELECT * FROM telemetry_links WHERE kind='telegram'")
    with telemetry.operation("unrelated.duplicate"):
        await telegram.state.receive(telegram_update())
    assert await db.one("SELECT * FROM telemetry_links WHERE kind='telegram'") == original
    monkeypatch.setattr(telegram, "_process", AsyncMock(side_effect=TimeoutError))
    await telegram.process_pending()
    event = await db.one("SELECT status,attempts FROM telegram_events")
    assert event == {"status": "pending", "attempts": 1}
    await telegram.close()
    await db.close()
    clock.advance(3)
    reopened = Database(db.root, clock)
    consumer = Telegram(reopened, settings, "789:TEST_FIXTURE_TOKEN")
    broker = ToolBroker(reopened, settings)

    class Backend(NativeBackend):
        name = "codex"

        async def execute(self, request, emit):
            # A new tool connection has no inherited asyncio trace context.
            result = await asyncio.create_task(
                broker.call(request.tool_token, "recall", {"query": "fixture"}),
                context=contextvars.Context(),
            )
            assert result.status == "ok"
            await emit("text_delta", {"text": "Fixture response"})
            return ExecutionOutcome(
                status=Outcome.COMPLETED,
                text="Fixture response",
                input_tokens=10,
                output_tokens=5,
            )

    try:
        with telemetry.operation("unrelated.restart"):
            await consumer.process_pending()
        job = await Jobs(reopened, settings.owner_id).claim("interactive", "fixture")
        assert job
        coordinator = Coordinator(
            reopened,
            settings,
            broker,
            tmp_path / "unused",
            factory=lambda _: Backend(reopened, settings),
        )
        await coordinator.run_job(job)
        sender = AsyncMock(return_value={"message_id": 1234, "chat_id": 123})
        with telemetry.operation("unrelated.delivery"):
            assert await Delivery(reopened, settings).dispatch_one(sender)
        sender.assert_awaited_once()
        finished = signals[0].get_finished_spans()
        for name in ("telegram.process", "job.run", "ai.run", "tool.call", "delivery.send"):
            matching = [span for span in finished if span.name == name]
            assert matching, name
            assert all(span.context.trace_id == expected for span in matching), name
        tool = next(span for span in finished if span.name == "tool.call")
        ai = next(span for span in finished if span.name == "ai.run")
        assert tool.parent.span_id == ai.context.span_id
        assert any(
            span.name == "telegram.process" and span.attributes["outcome"] == "retry"
            for span in finished
        )
    finally:
        await broker.close()
        await consumer.close()
        await reopened.close()


async def test_album_trace_joins_first_receipt_and_links_other_receipts(db, clock, signals):
    settings = Settings(
        encrypted_storage_verified=True, telegram_owner_id=123, telegram_chat_id=123
    )
    telegram = Telegram(db, settings, "789:TEST_FIXTURE_TOKEN")
    trace_ids = []
    try:
        for number in (1, 2):
            with telemetry.operation("fixture.album_part") as ingress:
                trace_ids.append(ingress.get_span_context().trace_id)
                await telegram.ingest(telegram_update(number, album=True))
        assert not await db.one("SELECT id FROM jobs")
        clock.advance(2)
        with telemetry.operation("unrelated.album_flush"):
            await telegram.state.flush_albums()
        saved = await db.one("SELECT traceparent FROM telemetry_links WHERE kind='job'")
        assert saved
        with telemetry.operation("fixture.album_job", upstream=saved["traceparent"]) as job:
            assert job.get_span_context().trace_id == trace_ids[0]
        album = next(
            span for span in signals[0].get_finished_spans() if span.name == "telegram.album"
        )
        assert album.context.trace_id == trace_ids[0]
        assert [link.context.trace_id for link in album.links] == trace_ids[1:]
    finally:
        await telegram.close()
