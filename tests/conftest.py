import os
import typing

# Workaround: pydantic 2.12 passes `prefer_fwd_module` to `typing._eval_type`
# for Python >= 3.14, but CPython 3.14.0rc2 doesn't have that parameter yet.
# Patch _eval_type to absorb the unknown kwarg until pydantic ships a fix.
_original_eval_type = typing._eval_type


def _patched_eval_type(*args: object, **kwargs: object) -> object:
    kwargs.pop("prefer_fwd_module", None)
    return _original_eval_type(*args, **kwargs)


typing._eval_type = _patched_eval_type  # type: ignore[attr-defined]
# Stub out mlx if not available (Apple Silicon only) so modules that
# import theo.embeddings can be collected on any platform.
try:
    import mlx.core  # noqa: F401
except (ImportError, OSError):  # fmt: skip
    import sys
    import types
    from unittest.mock import MagicMock

    _mlx = types.ModuleType("mlx")
    _mlx_core = MagicMock()
    _mlx_nn = MagicMock()
    _mlx_linalg = MagicMock()
    _mlx.core = _mlx_core  # type: ignore[attr-defined]
    _mlx.nn = _mlx_nn  # type: ignore[attr-defined]
    _mlx_core.linalg = _mlx_linalg
    sys.modules["mlx"] = _mlx
    sys.modules["mlx.core"] = _mlx_core
    sys.modules["mlx.nn"] = _mlx_nn

# Provide a default so get_settings() doesn't fail during test collection.
# Tests that construct Settings explicitly pass their own values.
os.environ.setdefault("THEO_DATABASE_URL", "postgresql://theo:test@localhost:5432/theo")
os.environ.setdefault("THEO_ANTHROPIC_API_KEY", "sk-ant-test-key")
