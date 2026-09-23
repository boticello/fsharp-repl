#!/usr/bin/env python3
"""fsi-send — client for the fsrepl broker.

    fsi-send.py SOCKET eval '<f# code>'   # one submission, structured result
    fsi-send.py SOCKET eval -             # full source from stdin
    fsi-send.py SOCKET eval --file PATH   # native #load of a script file
    fsi-send.py SOCKET ping               # liveness probe
    fsi-send.py TAIL <transcript.ndjson>  # pretty-print live transcript

The eval result prints: the value (when the code was an expression), captured
stdout, diagnostics and exceptions. Exit status is 1 when ok is false —
agents can branch on it. The tail mode prints the last few events and follows
(Ctrl-C stops watching; the broker keeps running).
"""

import json
import os
from pathlib import Path
import socket
import sys
import time


def send(sock_path: str, op: str, code: str) -> int:
    timeout = float(os.environ.get("FSI_SEND_TIMEOUT", "120"))
    req = {"id": f"send-{os.getpid()}-{int(time.time())}", "op": op, "code": code}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (OSError, TimeoutError) as exc:
        print(f"error: broker connection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        s.close()

    if not data:
        print(f"error: no response within {timeout:.0f}s — "
              f"the evaluation may still be running; watch the transcript", file=sys.stderr)
        return 1

    try:
        resp = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"error: invalid broker response: {exc}", file=sys.stderr)
        return 1
    if "ping" in resp:
        print(resp["ping"])
        return 0

    if resp.get("value") is not None:
        print(resp["value"])
    if resp.get("stdout"):
        print(resp["stdout"].rstrip("\n"))
    if resp.get("stderr"):
        print(resp["stderr"].rstrip("\n"), file=sys.stderr)
    for d in resp.get("diagnostics") or []:
        print(d, file=sys.stderr)
    if resp.get("exception"):
        print(f"exception: {resp['exception']}", file=sys.stderr)
    return 0 if resp.get("ok") else 1


def fmt_event(ev: dict) -> str:
    ts = (ev.get("ts") or "")[11:19]
    code = (ev.get("code") or ev.get("event") or "").replace("\n", " ")
    status = "ok  " if ev.get("ok") else ("FAIL" if "ok" in ev else "info")
    lines = [f"[{ts}] {status} {code[:100]}"]
    if ev.get("value") is not None:
        lines.append(f"        = {ev['value']}")
    for out in (ev.get("stdout") or "").rstrip("\n").splitlines():
        lines.append(f"        | {out}")
    for err in (ev.get("stderr") or "").rstrip("\n").splitlines():
        lines.append(f"        ! {err}")
    for d in ev.get("diagnostics") or []:
        lines.append(f"        ! {d}")
    if ev.get("exception"):
        lines.append(f"        x {ev['exception']}")
    return "\n".join(lines)


def tail(transcript: str) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    if not os.path.exists(transcript):
        print(f"no transcript yet at {transcript}", file=sys.stderr)
        return 1
    with open(transcript) as f:
        for line in open(transcript, encoding="utf-8-sig").readlines()[-20:]:
            try:
                print(fmt_event(json.loads(line)))
            except json.JSONDecodeError:
                pass
    print("--- following (Ctrl-C to stop watching; the session keeps running) ---")
    try:
        with open(transcript, encoding="utf-8-sig") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    try:
                        print(fmt_event(json.loads(line)))
                    except json.JSONDecodeError:
                        pass
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "TAIL":
        return tail(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[2] == "ping":
        return send(sys.argv[1], "ping", "")
    if len(sys.argv) == 4 and sys.argv[2] == "eval":
        source = sys.argv[3]
        if source == "-":
            try:
                source = sys.stdin.buffer.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                print(f"error: stdin is not UTF-8: {exc}", file=sys.stderr)
                return 1
        return send(sys.argv[1], "eval", source)
    if len(sys.argv) == 5 and sys.argv[2:4] == ["eval", "--file"]:
        try:
            path = Path(sys.argv[4]).resolve(strict=True)
            if not path.is_file():
                raise OSError(f"not a regular file: {path}")
        except OSError as exc:
            print(f"error: cannot load F# script: {exc}", file=sys.stderr)
            return 1
        return send(sys.argv[1], "eval", "#load " + json.dumps(str(path), ensure_ascii=False))
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
