#!/usr/bin/env python3
"""Launch and signal only the broker process recorded by this REPL session."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def signature(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def recorded_process(path: Path) -> int | None:
    try:
        record = json.loads(path.read_text())
        pid = record["pid"]
        if type(pid) is not int or pid <= 0 or not isinstance(record["signature"], str):
            return None
        return pid if signature(pid) == record["signature"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def launch(root: str, tool_root: str, sock: str, transcript: str, serverlog: str, pidfile: Path) -> None:
    env = os.environ.copy()
    env.update(FSREPL_ROOT=root, FSREPL_SOCKET=sock, FSREPL_TRANSCRIPT=transcript)
    with open(serverlog, "ab", buffering=0) as log:
        process = subprocess.Popen(
            ["dotnet", str(Path(tool_root) / "dotnet/tools/fsrepl/bin/Debug/net10.0/fsrepl.dll")],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    identity = signature(process.pid)
    if identity is None:
        raise RuntimeError("broker exited before its process identity could be recorded")
    record = {"pid": process.pid, "signature": identity}
    descriptor, temporary = tempfile.mkstemp(prefix="pid.", dir=pidfile.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output)
        os.replace(temporary, pidfile)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(process.pid)


def main() -> int:
    operation = sys.argv[1]
    if operation == "launch":
        launch(*sys.argv[2:7], Path(sys.argv[7]))
        return 0
    path = Path(sys.argv[2])
    pid = recorded_process(path)
    if pid is None:
        return 1
    if operation == "check":
        print(pid)
    elif operation == "signal":
        os.kill(pid, signal.SIGTERM if sys.argv[3] == "TERM" else signal.SIGKILL)
    else:
        raise ValueError(f"unknown operation: {operation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
