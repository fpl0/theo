"""Compatibility entry point for the independent local health observer.

Existing service definitions invoke python -m theo.observer. The implementation
lives in observability.observer so instrumentation has a single package owner.
"""

from theo.observability.observer import main

if __name__ == "__main__":
    main()
