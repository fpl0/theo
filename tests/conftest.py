import os
import typing

# Workaround: pydantic 2.12 passes `prefer_fwd_module` to `typing._eval_type`
# for Python >= 3.14, but CPython 3.14.0rc2 doesn't have that parameter yet.
# Patch _eval_type to absorb the unknown kwarg until pydantic ships a fix.
_original_eval_type = typing._eval_type  # noqa: SLF001


def _patched_eval_type(*args: object, **kwargs: object) -> object:
    kwargs.pop("prefer_fwd_module", None)
    return _original_eval_type(*args, **kwargs)


typing._eval_type = _patched_eval_type  # type: ignore[attr-defined]  # noqa: SLF001

# Provide a default so get_settings() doesn't fail during test collection.
# Tests that construct Settings explicitly pass their own values.
os.environ.setdefault("THEO_DATABASE_URL", "postgresql://theo:test@localhost:5432/theo")
