"""Deleting tests or weakening candidate configuration cannot redefine the pinned floor."""

import os

import pytest

from theo.domain import Denied
from theo.maintenance.minimum import acceptance_files, overlay


def test_minimum_tree_retains_prior_tests_and_tooling_with_candidate_implementation():
    baseline = {
        "src/theo/example.py": (b"old implementation", 0o644),
        "tests/test_regression.py": (b"assert important_contract()", 0o644),
        "tests/conftest.py": (b"deny_network()", 0o644),
        "pyproject.toml": (b"strict checks", 0o644),
        "uv.lock": (b"pinned testing dependencies", 0o644),
    }
    candidate = {
        "src/theo/example.py": (b"new implementation", 0o755),
        "tests/conftest.py": (b"skip_everything()", 0o644),
        "tests/test_new.py": (b"new candidate test", 0o644),
        "pyproject.toml": (b"disable checks", 0o644),
        "pytest.ini": (b"skip old tests", 0o644),
        "ruff.toml": (b"ignore everything", 0o644),
        "uv.lock": (b"candidate dependency update", 0o644),
        "docs/tools.md": (b"updated documentation", 0o644),
    }
    minimum = overlay(baseline, candidate)
    assert minimum["src/theo/example.py"] == candidate["src/theo/example.py"]
    assert minimum["docs/tools.md"] == candidate["docs/tools.md"]
    for name in ("tests/test_regression.py", "tests/conftest.py", "pyproject.toml", "uv.lock"):
        assert minimum[name] == baseline[name]
    assert not {"pytest.ini", "ruff.toml", "tests/test_new.py"} & minimum.keys()
    assert candidate["tests/conftest.py"][0] == b"skip_everything()"


@pytest.mark.skipif(os.geteuid() == 0, reason="Requires a user-owned source fixture")
def test_unprivileged_source_cannot_claim_to_be_the_minimum_recipe(tmp_path):
    baseline = tmp_path / "source"
    baseline.mkdir()
    with pytest.raises(Denied):
        acceptance_files(baseline, "a" * 64, {})
