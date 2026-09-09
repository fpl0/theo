"""Console entry point for Theo operator commands.

Parses arguments, runs the selected command and renders structured errors;
command implementations and credential resolution live in sibling modules.
"""

import asyncio
import json
import sqlite3
import sys

from theo.domain import TheoError


def main() -> None:
    from theo.cli.commands import execute
    from theo.cli.parser import parser

    args = parser().parse_args()
    try:
        result = asyncio.run(execute(args))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (TheoError, ValueError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
