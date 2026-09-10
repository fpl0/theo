import importlib.util
from pathlib import Path


def test_external_interpreter_link_becomes_a_hashed_release_file(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "release_builder", Path(__file__).parents[1] / "scripts/build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "system-python"
    source.write_bytes(b"synthetic executable")
    source.chmod(0o755)
    release = tmp_path / "release"
    release.mkdir()
    binary = release / "python"
    binary.symlink_to(source)
    module.materialize_external_links(release)
    assert not binary.is_symlink()
    assert binary.read_bytes() == source.read_bytes()
    assert binary.stat().st_mode & 0o111
    source.write_bytes(b"upgraded system executable")
    assert binary.read_bytes() == b"synthetic executable"
