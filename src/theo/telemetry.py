"""OpenTelemetry SDK bootstrap — traces, metrics, and logs."""

from __future__ import annotations

import importlib
import logging
import socket

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from theo import __version__
from theo.config import get_settings

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_logger_provider: LoggerProvider | None = None

# OTLP imports are deferred because they pull in protobuf + requests.
_OTLP_FACTORIES: dict[str, str] = {
    "span": "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "metric": "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "log": "opentelemetry.exporter.otlp.proto.http._log_exporter",
}


def _build_exporters(
    kind: str,
) -> tuple[ConsoleSpanExporter, ConsoleMetricExporter, ConsoleLogRecordExporter]:
    """Return (span, metric, log) exporters for the chosen backend."""
    if kind == "otlp":
        span_mod = importlib.import_module(_OTLP_FACTORIES["span"])
        metric_mod = importlib.import_module(_OTLP_FACTORIES["metric"])
        log_mod = importlib.import_module(_OTLP_FACTORIES["log"])
        return (
            span_mod.OTLPSpanExporter(),
            metric_mod.OTLPMetricExporter(),
            log_mod.OTLPLogExporter(),
        )

    return ConsoleSpanExporter(), ConsoleMetricExporter(), ConsoleLogRecordExporter()


def init_telemetry() -> None:
    """Initialise all three OTEL signals and instrument asyncpg.

    Safe to call multiple times — only the first call takes effect.
    """
    global _tracer_provider, _meter_provider, _logger_provider  # noqa: PLW0603
    if _tracer_provider is not None:
        return

    cfg = get_settings()
    resource = Resource.create({
        "service.name": "theo",
        "service.version": __version__,
        "host.name": socket.gethostname(),
    })

    span_exporter, metric_exporter, log_exporter = _build_exporters(cfg.otel_exporter)

    # --- Traces ---
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    reader = PeriodicExportingMetricReader(metric_exporter)
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)

    # --- Logs (bridge stdlib logging → OTEL) ---
    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(_logger_provider)

    handler = LoggingHandler(level=logging.DEBUG, logger_provider=_logger_provider)
    logging.getLogger().addHandler(handler)

    # --- Auto-instrument asyncpg ---
    AsyncPGInstrumentor().instrument(sanitize_query=True)


def shutdown_telemetry() -> None:
    """Flush and shut down all OTEL providers. Safe to call if not initialised."""
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    if _meter_provider is not None:
        _meter_provider.shutdown()
    if _logger_provider is not None:
        _logger_provider.shutdown()
