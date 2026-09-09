"""Private token-file handling for the dedicated local Telegram test runner."""

import importlib.util
import stat
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "telegram_setup", Path(__file__).parents[1] / "scripts/telegram_setup.py"
)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_saved_test_token_is_private_and_cannot_be_replaced(tmp_path):
    token_file = tmp_path / "private/token"
    setup.save_private_token(token_file, "synthetic token one")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert setup.read_private_token(token_file) == "synthetic token one"
    setup.save_private_token(token_file, "synthetic token one")
    with pytest.raises(ValueError, match="replace"):
        setup.save_private_token(token_file, "synthetic token two")
    assert setup.read_private_token(token_file) == "synthetic token one"


def test_token_file_refuses_shared_permissions_and_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text("synthetic token")
    target.chmod(0o644)
    with pytest.raises(ValueError, match="no group/other access"):
        setup.read_private_token(target)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        setup.read_private_token(link)
    with pytest.raises(OSError):
        setup.save_private_token(link, "synthetic token")
    assert target.read_text() == "synthetic token"
