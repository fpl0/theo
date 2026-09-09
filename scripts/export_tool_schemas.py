"""Regenerate the published model-tool reference from the actual strict catalog."""

import json
from pathlib import Path

from theo.tools.registry import BASELINE, REGISTRY


def main() -> None:
    definitions = [
        {
            "name": name,
            "baseline": name in BASELINE,
            "description": definition.description,
            "inputSchema": definition.schema.model_json_schema(),
        }
        for name, definition in REGISTRY.items()
    ]
    target = Path(__file__).resolve().parents[1] / "docs/tool-schemas.json"
    target.write_text(json.dumps(definitions, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(definitions)} tool definitions to {target}")


if __name__ == "__main__":
    main()
