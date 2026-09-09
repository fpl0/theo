import stat

import pytest

from theo.memory.store import Memory
from theo.operations.export import export_data


@pytest.mark.parametrize("format", ["jsonl", "markdown"])
async def test_export_failure_preserves_previous_complete_file(db, tmp_path, monkeypatch, format):
    destination = tmp_path / "exports"
    destination.mkdir()
    target = destination / "memories"
    target.write_text("previous complete export")
    await Memory(db, "owner").remember("private memory", source="owner")

    def fail_sync(fd):
        raise OSError("storage failure")

    monkeypatch.setattr("theo.operations.export.os.fsync", fail_sync)
    with pytest.raises(OSError, match="storage failure"):
        await export_data(db, target, format)
    assert target.read_text() == "previous complete export"
    assert list(destination.iterdir()) == [target]


async def test_export_is_private_before_sensitive_data_is_written(db, tmp_path, monkeypatch):
    from theo.operations import export

    destination = tmp_path / "exports"
    destination.mkdir()
    target = destination / "memories.jsonl"
    await Memory(db, "owner").remember("private memory", source="owner")
    original_encode = export.encode
    checked = False

    def inspect_write(value):
        nonlocal checked
        for path in destination.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            checked = True
        return original_encode(value)

    monkeypatch.setattr(export, "encode", inspect_write)
    assert await export_data(db, target) == target
    assert checked
    assert "private memory" in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(destination.iterdir()) == [target]
