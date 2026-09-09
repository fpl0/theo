"""Native subscription workers with one protocol adapter per runtime.

Base owns eligibility and event lifecycle; Claude, Codex and ACP modules own wire
protocols. Factory selects an adapter, while policy manages account evidence.
"""
