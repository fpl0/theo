"""Build a relocatable locked release with an installed application and local startup canary."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from theo import __version__
from theo.execution.files import file_hash


def build(source: Path, destination: Path, release_id: str) -> None:
    if destination.exists():
        raise ValueError("Release destination must be new")
    if subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip():
        raise ValueError("Commit reviewed source before building an immutable release")
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    destination.mkdir(parents=True)
    try:
        subprocess.run(
            ["uv", "venv", "--relocatable", "--python", "3.14", str(destination)], check=True
        )
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(destination)}
        subprocess.run(
            ["uv", "sync", "--project", str(source), "--frozen", "--no-dev", "--no-editable"],
            env=env,
            check=True,
        )
        python = destination / "bin/python"
        with tempfile.TemporaryDirectory(prefix="theo-release-canary-") as tmp:
            for arguments in (("init",), ("doctor", "--json")):
                result = subprocess.run(
                    [str(python), "-m", "theo", "--data-root", tmp, *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                json.loads(result.stdout)
        shutil.copyfile(source / "uv.lock", destination / "uv.lock")
        shutil.copyfile(source / "docs/compatibility.json", destination / "compatibility.json")
        files = {
            str(path.relative_to(destination)): file_hash(path)
            for path in destination.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        schema = len(list((source / "src/theo/migrations").glob("*.sql")))
        manifest = {
            "id": release_id,
            "version": __version__,
            "source_commit": commit,
            "lock_sha256": file_hash(source / "uv.lock"),
            "schema_min": schema,
            "schema_max": schema,
            "files": files,
            "canary_passed": True,
        }
        (destination / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "release": str(destination),
                    "source_commit": commit,
                    "canary": "local init and doctor; native and Mac qualification separate",
                }
            )
        )
    except BaseException:
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--id", required=True)
    args = parser.parse_args()
    build(args.source.resolve(), args.destination.resolve(), args.id)
