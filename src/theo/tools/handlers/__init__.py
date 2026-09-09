"""Capability implementations invoked after host authorization.

Handlers receive ToolCall rather than the broker so they cannot create grants,
change schemas or bypass receipt reservation for model invocations.
"""
