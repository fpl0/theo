"""Opt-in tracing, metrics and bounded structured operational logs.

Configures local and OTLP exporters, propagates trace context and restricts emitted
attributes to an allowlist. Conversation content is outside the telemetry contract.
"""

import atexit
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import time
from collections.abc import Callable, Coroutine, Generator, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

import psutil
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import DefaultGetter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from theo import __version__

_provider: TracerProvider | None = None
_metrics: MeterProvider | None = None
_logs: LoggerProvider | None = None
_instruments: dict[tuple[str, str], Any] = {}
_logger = logging.getLogger("theo.telemetry.events")
_propagator = TraceContextTextMapPropagator()
_outcome = contextvars.ContextVar("telemetry_outcome", default="success")
_labels = {
    "component",
    "operation",
    "channel",
    "backend",
    "outcome",
    "tool",
    "kind",
    "token_type",
    "window",
}
_fields = _labels | {"job_id", "run_id", "error_type", "duration_seconds", "model", "event"}


def enabled() -> bool:
    return _provider is not None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": record.created,
                "level": record.levelname,
                **getattr(record, "safe_fields", {}),
                "event": record.getMessage(),
            }
        )


def configure(root: Path, service: str = "theo", *, force: bool = False) -> None:
    # Observability must never prevent the native service from starting.
    try:
        _configure(root, service, force=force)
    except OSError, ValueError, TypeError:
        shutdown()
        logging.getLogger("theo").warning(
            "Telemetry configuration unavailable; native service continues"
        )


def _configure(root: Path, service: str, *, force: bool) -> None:
    global _provider, _metrics, _logs
    if _provider:
        return
    options: dict[str, Any] = {}
    path = root / "telemetry.json"
    if path.exists():
        loaded = json.loads(path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("Expected telemetry settings object")
        options = cast(dict[str, Any], loaded)
    if (
        not force
        and os.getenv("THEO_TELEMETRY_ENABLED", "1" if options.get("enabled") is True else "0")
        != "1"
    ):
        return
    endpoint = os.getenv(
        "THEO_OTLP_ENDPOINT", options.get("endpoint", "http://127.0.0.1:14318")
    ).rstrip("/")
    resource = Resource.create(
        {
            "service.name": service,
            "service.version": __version__,
            "deployment.environment.name": os.getenv(
                "THEO_ENVIRONMENT", options.get("environment", "local")
            ),
        }
    )
    _provider = TracerProvider(
        resource=resource,
        shutdown_on_exit=False,
        sampler=ParentBased(
            TraceIdRatioBased(
                float(os.getenv("THEO_TRACE_SAMPLE_RATIO", str(options.get("sample_ratio", 0.1))))
            )
        ),
    )
    _metrics = MeterProvider(
        resource=resource,
        shutdown_on_exit=False,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint + "/v1/metrics", timeout=2),
                export_interval_millis=5000,
                export_timeout_millis=3000,
            )
        ],
        views=[
            View(
                instrument_name="*_duration",
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900]
                ),
            )
        ],
    )
    os.environ.setdefault("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    _provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint + "/v1/traces", timeout=2),
            max_queue_size=512,
            max_export_batch_size=64,
            schedule_delay_millis=1000,
            meter_provider=_metrics,
        )
    )
    _logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
    _logs.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=endpoint + "/v1/logs", timeout=2),
            max_queue_size=512,
            max_export_batch_size=64,
            schedule_delay_millis=1000,
            meter_provider=_metrics,
        )
    )
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    _logger.addHandler(LoggingHandler(logger_provider=_logs))
    directory = root / "telemetry"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Retain recent diagnostic files across short-lived CLI processes, at most 30 MB.
    total = 0
    for previous in sorted(
        directory.glob("*-*.jsonl*"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        pid = re.search(r"-(\d+)\.jsonl", previous.name)
        if pid and psutil.pid_exists(int(pid[1])):
            continue
        total += previous.stat().st_size
        if total > 30_000_000 or time.time() - previous.stat().st_mtime > 3 * 86400:
            with contextlib.suppress(OSError):
                previous.unlink()
    file = RotatingFileHandler(
        directory / f"{service}-{os.getpid()}.jsonl", maxBytes=2_000_000, backupCount=2
    )
    file.setFormatter(JsonFormatter())
    _logger.addHandler(file)
    atexit.register(shutdown)


def shutdown() -> None:
    global _provider, _metrics, _logs
    providers = (_logs, _provider, _metrics)
    _provider = _metrics = _logs = None
    for provider in providers:
        if provider:
            with contextlib.suppress(Exception):
                provider.shutdown()
    _instruments.clear()
    for handler in list(_logger.handlers):
        handler.close()
        _logger.removeHandler(handler)


def carrier() -> str:
    value: dict[str, str] = {}
    _propagator.inject(value)
    return value.get("traceparent", "")


def parent(value: str) -> Context:
    return _propagator.extract({"traceparent": value}, getter=DefaultGetter())


def event(name: str, **fields: Any) -> None:
    if not enabled():
        return
    selected = {
        key: _bounded(value)
        for key, value in fields.items()
        if key in _fields and value is not None
    }
    selected["event"] = name
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        selected.update(
            trace_id=f"{ctx.trace_id:032x}",
            span_id=f"{ctx.span_id:016x}",
            trace_sampled=ctx.trace_flags.sampled,
        )
    if ctx.is_valid and ctx.trace_flags.sampled:
        selected["sampled_trace_id"] = f"{ctx.trace_id:032x}"
    level = (
        logging.ERROR
        if name.endswith(".failed") or fields.get("outcome") in {"failed", "error", "uncertain"}
        else logging.INFO
    )
    _logger.log(level, name, extra={"safe_fields": selected, **selected})


def mark_outcome(value: str) -> None:
    _outcome.set(value)
    span = trace.get_current_span()
    span.set_attribute("outcome", value)
    if value in {"failed", "denied", "invalid", "error", "uncertain"}:
        span.set_status(trace.Status(trace.StatusCode.ERROR, value))


def measure(
    name: str, value: float = 1, *, histogram: bool = False, gauge: bool = False, **labels: str
) -> None:
    if not _metrics:
        return
    attributes = {k: v[:120] for k, v in labels.items() if k in _labels}
    key = (name, "histogram" if histogram else "gauge" if gauge else "counter")
    if key not in _instruments:
        meter = _metrics.get_meter("theo")
        _instruments[key] = (
            meter.create_histogram(name, unit="s")
            if histogram
            else meter.create_gauge(name)
            if gauge
            else meter.create_counter(name)
        )
    instrument = _instruments[key]
    if histogram:
        instrument.record(value, attributes)
    elif gauge:
        instrument.set(value, attributes)
    else:
        instrument.add(value, attributes)


@contextlib.contextmanager
def operation(
    name: str, *, upstream: str = "", links: Sequence[str] = (), **attributes: Any
) -> Generator[trace.Span]:
    tracer = _provider.get_tracer("theo") if _provider else trace.NoOpTracer()
    safe = {k: _bounded(v) for k, v in attributes.items() if k in _fields and v is not None}
    with tracer.start_as_current_span(
        name,
        context=parent(upstream) if upstream else None,
        attributes=safe,
        links=[
            trace.Link(ctx)
            for value in links[:64]
            if (ctx := trace.get_current_span(parent(value)).get_span_context()).is_valid
        ],
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        start = time.monotonic()
        outcome = "success"
        outcome_token = _outcome.set("success")
        event(name + ".started", **safe)
        try:
            yield span
        except BaseException as exc:
            outcome = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
            span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
            event(name + ".failed", error_type=type(exc).__name__, **safe)
            raise
        finally:
            duration = time.monotonic() - start
            if outcome == "success":
                outcome = _outcome.get()
            _outcome.reset(outcome_token)
            labels = {k: str(v) for k, v in safe.items() if k in _labels and k != "outcome"}
            measure(
                "theo_operation_duration",
                duration,
                histogram=True,
                gauge=False,
                operation=name,
                outcome=outcome,
                **{k: v for k, v in labels.items() if k != "operation"},
            )
            event(
                name + ".finished",
                duration_seconds=duration,
                outcome=outcome,
                **{k: v for k, v in safe.items() if k != "outcome"},
            )


def observed(name: str, **labels: str) -> Callable[..., Any]:
    def decorate(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            with operation(name, **labels):
                return await fn(*args, **kwargs)

        return wrapped

    return decorate


def _bounded(value: Any) -> str | bool | int | float:
    return value if isinstance(value, (bool, int, float)) else str(value)[:256]
