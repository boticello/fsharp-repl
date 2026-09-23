#!/usr/bin/env python3
"""Read a consuming project's optional fsrepl.json configuration."""

import json
from pathlib import Path
import subprocess
import sys


def configuration(root: Path) -> dict:
    path = root / "fsrepl.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    preload = data.get("preload")
    references = data.get("references", [])
    build = data.get("build", [])
    if preload is not None and (not isinstance(preload, str) or not preload):
        raise ValueError("preload must be a non-empty path")
    if not isinstance(references, list) or any(not isinstance(x, str) or not x for x in references):
        raise ValueError("references must be a list of paths")
    if not isinstance(build, list) or any(not isinstance(x, str) or not x for x in build):
        raise ValueError("build must be an argv array")
    return {"preload": preload, "references": references, "build": build}


def prepare(root: Path) -> dict:
    data = configuration(root)
    if data["build"]:
        subprocess.run(data["build"], cwd=root, check=True)
    paths = ([data["preload"]] if data["preload"] else []) + data["references"]
    for relative in paths:
        path = (root / relative).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"configured REPL input does not exist: {path}")
    return data


def main() -> int:
    root = Path(sys.argv[2]).resolve()
    data = configuration(root)
    operation = sys.argv[1]
    if operation == "preload":
        if data["preload"]:
            print((root / data["preload"]).resolve())
        return 0
    if operation == "prepare":
        prepare(root)
        return 0
    raise ValueError(f"unknown operation: {operation}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"fsrepl: {error}", file=sys.stderr)
        sys.exit(1)
