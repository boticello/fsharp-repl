#!/bin/sh
# fsi-repl — a shared, persistent F# REPL for the current project.
#
#   scripts/fsi-repl.sh start         # boot the broker (background): one
#                                     #   long-lived hosted fsi session; an
#                                     #   optional preload and state persist
#   scripts/fsi-repl.sh send '<code>' # one submission through the queue;
#                                     #   prints the value/stdout/diagnostics
#   scripts/fsi-repl.sh send -        # read one submission from stdin
#   scripts/fsi-repl.sh send --file path.fsx  # native FSI #load
#   scripts/fsi-repl.sh log           # pretty-print the live transcript (run
#                                     #   in any number of terminals — this is
#                                     #   how you watch the agent work, or
#                                     #   follow along with its exploration)
#   scripts/fsi-repl.sh ping          # liveness probe
#   scripts/fsi-repl.sh status | stop
#
# All submissions — agent or human, any terminal — serialize through the one
# broker, so evaluations never interleave and everything lands in the
# transcript (repo-local .fsrepl/transcript.ndjson, gitignored). Recovery from
# a runaway evaluation is stop/start; durable state belongs in checkpoint
# scripts, not the process.

set -eu

tool_root=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "${FSREPL_PROJECT_ROOT:-.}" && pwd)
name=$(python3 - "$root" <<'PY'
import hashlib
import os
import sys
identity = f"{os.getuid()}:{sys.argv[1]}".encode()
print("f-" + hashlib.sha256(identity).hexdigest()[:16])
PY
)
tmpdir="${FSREPL_RUNTIME_DIR:-/tmp}"
tmpdir="${tmpdir%/}"
sock="$tmpdir/$name.sock"
if [ "$(printf %s "$sock" | wc -c | tr -d ' ')" -gt 104 ]; then
    tmpdir=/tmp
    sock="$tmpdir/$name.sock"
fi
serverlog="$tmpdir/$name.server.log"
state_dir="${FSREPL_STATE_DIR:-$root/.fsrepl}"
pidfile="$state_dir/pid"
transcript="$state_dir/transcript.ndjson"

# Lifecycle mutations must be serial across agents. Keep the advisory lock
# open in the parent Python process while this script runs as its child.
# fcntl releases it even if the child fails or is interrupted.
case "${1:-}" in
    start|stop)
        if [ "${FSREPL_LOCK_HELD:-}" != 1 ]; then
            mkdir -p "$state_dir"
            exec python3 - "$state_dir/lifecycle.lock" "$0" "$@" <<'PY'
import fcntl
import os
import subprocess
import sys

with open(sys.argv[1], "a+b") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    env = os.environ.copy()
    env["FSREPL_LOCK_HELD"] = "1"
    sys.exit(subprocess.call(sys.argv[2:], env=env))
PY
        fi
        ;;
esac

alive() {
    [ -f "$pidfile" ] && python3 "$tool_root/scripts/fsi-process.py" check "$pidfile" >/dev/null
}

broker_pid() {
    python3 "$tool_root/scripts/fsi-process.py" check "$pidfile"
}

socket_has_listener() {
    python3 - "$sock" <<'PY'
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(2)
try:
    client.connect(sys.argv[1])
except (ConnectionRefusedError, FileNotFoundError):
    sys.exit(1)  # An abandoned socket file may be removed.
except OSError as error:
    print(f"error: cannot determine socket owner: {error}", file=sys.stderr)
    sys.exit(2)
else:
    # Complete a valid request so the broker does not log an empty connection.
    try:
        client.sendall(b'{"id":"socket-probe","op":"ping","code":""}\n')
        client.recv(4096)
    except OSError:
        pass  # A successful connect is enough to establish ownership.
finally:
    client.close()
PY
}

assert_socket_unowned() {
    [ -S "$sock" ] || return 0
    if socket_has_listener; then
        echo "error: a broker still owns $sock; refusing to unlink its socket" >&2
        return 1
    else
        result=$?
        [ "$result" -eq 1 ] || return 1
    fi
}

ready() {
    alive && [ -S "$sock" ] && FSI_SEND_TIMEOUT=3 python3 "$tool_root/scripts/fsi-send.py" "$sock" ping >/dev/null 2>&1
}

cmd_start() {
    if ready; then
        echo "already running (pid $(broker_pid))"
        exit 0
    fi
    if alive; then
        echo "error: broker process exists but does not answer ping (pid $(broker_pid))" >&2
        exit 1
    fi

    python3 "$tool_root/scripts/fsi-config.py" prepare "$root"

    mkdir -p "$state_dir"
    mkdir -p "$tmpdir"
    assert_socket_unowned
    rm -f "$sock"
    : > "$serverlog"

    # Deterministic build of the broker (outside the solution; first build
    # restores FSharp.Compiler.Service, later builds are no-ops).
    (cd "$tool_root" && dotnet build dotnet/tools/fsrepl/fsrepl.fsproj --nologo -v q) \
        || { echo "error: broker build failed" >&2; exit 1; }

    # A separate session survives the calling terminal or agent command
    # ending. `nohup` alone only ignores SIGHUP and can leave the broker in
    # the caller's process group.
    python3 "$tool_root/scripts/fsi-process.py" launch "$root" "$tool_root" "$sock" "$transcript" "$serverlog" "$pidfile" >/dev/null

    waited=0
    while ! ready; do
        if ! alive; then
            echo "error: broker died at startup — $serverlog:" >&2
            tail -5 "$serverlog" >&2
            rm -f "$pidfile"
            exit 1
        fi
        [ "$waited" -ge 60 ] && { echo "error: broker did not become ready after 60s — $serverlog" >&2; exit 1; }
        sleep 2
        waited=$((waited + 2))
    done

    echo "session up (pid $(broker_pid))"
    echo "send: scripts/fsi-repl.sh send '<f# code>'    watch: scripts/fsi-repl.sh log"
    echo "transcript: $transcript"
}

cmd_stop() {
    if alive; then
        python3 "$tool_root/scripts/fsi-process.py" signal "$pidfile" TERM 2>/dev/null || true
        sleep 1
        if alive; then
            python3 "$tool_root/scripts/fsi-process.py" signal "$pidfile" KILL 2>/dev/null || true
        fi
    fi
    assert_socket_unowned
    rm -f "$pidfile" "$sock"
    echo "session stopped (transcript kept at $transcript)"
}

cmd_status() {
    if ready; then
        echo "up: pid $(broker_pid), socket $sock"
        [ -f "$transcript" ] && echo "transcript: $transcript ($(wc -l < "$transcript" | tr -d ' ') events)"
    else
        if alive; then
            echo "unresponsive: pid $(broker_pid), socket $sock" >&2
        else
            echo "down (start with: scripts/fsi-repl.sh start)"
        fi
        exit 1
    fi
}

cmd_send() {
    if [ "$#" -eq 1 ]; then
        mode=arg
    elif [ "$#" -eq 2 ] && [ "$1" = "--file" ]; then
        mode=file
    else
        echo "usage: $0 send '<f# code>' | - | --file PATH" >&2
        exit 1
    fi
    [ -S "$sock" ] || { echo "no session — start with: scripts/fsi-repl.sh start" >&2; exit 1; }
    if [ "$mode" = file ]; then
        python3 "$tool_root/scripts/fsi-send.py" "$sock" eval --file "$2"
    elif [ "$1" = - ]; then
        python3 "$tool_root/scripts/fsi-send.py" "$sock" eval -
    else
        python3 "$tool_root/scripts/fsi-send.py" "$sock" eval "$1"
    fi
}

case "${1:-}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    log)
        [ -f "$transcript" ] || { echo "no transcript — start a session first" >&2; exit 1; }
        exec python3 "$tool_root/scripts/fsi-send.py" TAIL "$transcript"
        ;;
    ping)
        [ -S "$sock" ] || { echo "no session" >&2; exit 1; }
        python3 "$tool_root/scripts/fsi-send.py" "$sock" ping
        ;;
    send) shift; cmd_send "$@" ;;
    *)
        echo "usage: $0 {start|send '<f# code>'|send -|send --file PATH|log|ping|status|stop}" >&2
        exit 1
        ;;
esac
