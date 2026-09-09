"""Guard dependency direction and the published model-tool contract."""

import ast
import json
import subprocess
import sys
from graphlib import TopologicalSorter
from pathlib import Path

import pytest

from theo.tools.registry import BASELINE, REGISTRY

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


@pytest.fixture(scope="module")
def imports():
    modules = {}
    for path in (SOURCE / "theo").rglob("*.py"):
        name = ".".join(path.relative_to(SOURCE).with_suffix("").parts)
        name = name.removesuffix(".__init__")
        modules[name] = ast.parse(path.read_text())
    graph = {}
    for name, tree in modules.items():
        dependencies = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(a.name for a in node.names if a.name.startswith("theo."))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "theo" or node.module.startswith("theo."):
                    dependencies.add(node.module)
                    dependencies.update(
                        f"{node.module}.{alias.name}"
                        for alias in node.names
                        if f"{node.module}.{alias.name}" in modules
                    )
        assert dependencies <= modules.keys(), (name, dependencies - modules.keys())
        graph[name] = dependencies
    return graph


def test_internal_module_imports_are_acyclic(imports):
    # Include deferred imports: moving an import inside a function must not be
    # used to hide a circular dependency between services.
    tuple(TopologicalSorter(imports).static_order())


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("theo.domain", ("theo.",)),
        ("theo.config", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        ("theo.storage", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        ("theo.memory", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        ("theo.work", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        ("theo.content", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        ("theo.delivery", ("theo.application", "theo.channels", "theo.cli", "theo.tools")),
        (
            "theo.tools.handlers",
            (
                "theo.application",
                "theo.channels",
                "theo.cli",
                "theo.tools.broker",
                "theo.tools.registry",
            ),
        ),
        (
            "theo.channels.telegram.sender",
            ("theo.work", "theo.delivery.ledger", "theo.channels.telegram.state"),
        ),
    ],
)
def test_services_do_not_depend_on_their_callers(imports, package, forbidden):
    for name, dependencies in imports.items():
        if name == package or name.startswith(package + "."):
            assert not any(dep.startswith(forbidden) for dep in dependencies), name


def test_cli_syntax_import_does_not_load_execution_services():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from theo.cli.parser import parser; import sys; "
            "assert parser().parse_args(['status']).command == 'status'; "
            "assert not any(m in sys.modules for m in "
            "('theo.application.service', 'theo.tools.broker', 'theo.backends.claude', "
            "'theo.backends.codex', 'theo.backends.acp', 'theo.cli.credentials'))",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_published_tool_schemas_match_the_catalog():
    documented = json.loads((ROOT / "docs/tool-schemas.json").read_text())
    assert documented == [
        {
            "name": name,
            "baseline": name in BASELINE,
            "description": definition.description,
            "inputSchema": definition.schema.model_json_schema(),
        }
        for name, definition in REGISTRY.items()
    ]
