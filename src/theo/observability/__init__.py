"""Operational instrumentation and the independent local observer.

Telemetry emits bounded allowlisted metadata; observer reads host and database
health without joining the core writer or worker authority boundary.
"""
