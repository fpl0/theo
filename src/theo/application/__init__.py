"""Application orchestration and daemon composition.

Service owns process lifetime, Coordinator owns leased attempts, and commands
and status serve host-owned requests without model inference.
"""
