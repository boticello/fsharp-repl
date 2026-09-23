#!/usr/bin/env python3
"""Run one FSI expression or script with the consuming project's references."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from fsi_config import prepare


def run() -> int:
    args = sys.argv[1:]
    if len(args) == 1 and args[0] == "-":
        mode = "stdin"
    elif len(args) == 1:
        mode = "expression"
    elif len(args) == 2 and args[0] == "--file":
        mode = "file"
    else:
        print("usage: fsi-eval.sh '<f# expression>' | - | --file PATH", file=sys.stderr)
        return 1

    root = Path(os.environ.get("FSREPL_PROJECT_ROOT", ".")).resolve()
    data = prepare(root)
    command = ["dotnet", "fsi"]
    command.extend(f"-r:{(root / path).resolve()}" for path in data["references"])
    if data["preload"]:
        command.append(f"--use:{(root / data['preload']).resolve()}")
    command.extend(["--exec", "--quiet"])

    if mode == "file":
        script = Path(args[1]).resolve(strict=True)
        if not script.is_file():
            raise OSError(f"not a regular F# script: {script}")
        return subprocess.run([*command, str(script)], cwd=root, check=False).returncode

    with tempfile.TemporaryDirectory(prefix="fsi-eval-") as directory:
        script = Path(directory) / "input.fsx"
        if mode == "stdin":
            script.write_bytes(sys.stdin.buffer.read())
        else:
            script.write_text(f'printfn "%A" ({args[0]})\n', encoding="utf-8")
        return subprocess.run([*command, str(script)], cwd=root, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"fsi-eval: {error}", file=sys.stderr)
        sys.exit(1)
