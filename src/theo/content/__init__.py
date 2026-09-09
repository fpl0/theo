"""Content acquisition and validation independent of conversation transport.

Owns immutable artifacts, guarded public-web reads and optional local media
processing; channel-specific receipt and routing logic lives in channels.
"""
