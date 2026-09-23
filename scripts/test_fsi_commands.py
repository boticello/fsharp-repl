#!/usr/bin/env python3
"""Public command tests run from a fresh consuming project's directory."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest


TOOL_ROOT = Path(__file__).resolve().parent.parent
REPL = TOOL_ROOT / "scripts/fsi-repl.sh"
EVAL = TOOL_ROOT / "scripts/fsi-eval.sh"
BROKER = TOOL_ROOT / "dotnet/tools/fsrepl/bin/Debug/net10.0/fsrepl.dll"


class ConsumerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fsrepl-consumer-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.env = {
            **os.environ,
            "TMPDIR": str(self.base),
            "FSREPL_STATE_DIR": str(self.state),
        }

    def run_command(self, *args, input=None, timeout=30, env=None):
        return subprocess.run(
            [str(arg) for arg in args],
            cwd=self.project,
            env=env or self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


class TranscriptTests(unittest.TestCase):
    def test_log_format_includes_stderr(self):
        path = TOOL_ROOT / "scripts/fsi-send.py"
        spec = importlib.util.spec_from_file_location("fsi_send", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rendered = module.fmt_event({
            "code": 'eprintfn "warning"', "ok": True, "stderr": "warning\n"
        })
        self.assertIn("! warning", rendered)


class OneRequestBroker:
    def __init__(self, path: Path):
        self.source = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        with self.server:
            client, _ = self.server.accept()
            with client:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                request = json.loads(data)
                self.source = request["code"]
                client.sendall((json.dumps({
                    "id": request["id"], "ok": True, "value": "accepted"
                }) + "\n").encode())


class CommandBoundaryTests(ConsumerCase):
    def test_public_runtime_directory_is_refused(self):
        env = {**self.env, "FSREPL_RUNTIME_DIR": "/tmp"}
        result = self.run_command(REPL, "status", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private directory", result.stderr)

    def check_source(self, command, expected, stdin=None, file_content=None):
        name = f"{os.getuid()}:{self.project.resolve()}".encode()
        sock = Path(f"/tmp/fsrepl-{os.getuid()}") / f"f-{hashlib.sha256(name).hexdigest()[:16]}.sock"
        sock.parent.mkdir(mode=0o700, exist_ok=True)
        self.addCleanup(sock.unlink, missing_ok=True)
        broker = OneRequestBroker(sock)
        if file_content is not None:
            source_file = self.project / "source file.fsx"
            source_file.write_bytes(file_content.encode("utf-8"))
            command = [str(source_file) if arg == "SOURCE_FILE" else arg for arg in command]
        result = self.run_command(*command, input=stdin)
        broker.thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "accepted")
        self.assertEqual(broker.source, expected(source_file) if callable(expected) else expected)

    def test_expression_preserves_shell_metacharacters(self):
        source = 'printfn "%s" "$(touch /no-such-path) `printf risky` ; café"'
        self.check_source([REPL, "send", source], source)

    def test_stdin_preserves_multiline_source(self):
        source = 'let greeting = "café"\r\nprintfn "%s" greeting\r\n'
        self.check_source([REPL, "send", "-"], source, stdin=source)

    def test_file_uses_native_load_with_absolute_path(self):
        source = 'module FeatureProbe\r\nlet answer = 19\r\n'
        self.check_source(
            [REPL, "send", "--file", "SOURCE_FILE"],
            lambda path: "#load " + json.dumps(str(path.resolve())),
            file_content=source,
        )

    def test_missing_session_is_a_clean_error(self):
        result = self.run_command(REPL, "send", "1 + 2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no session", result.stderr)

    def test_invalid_config_fails_before_starting(self):
        (self.project / "fsrepl.json").write_text('{"references": ["missing.dll"]}')
        result = self.run_command(REPL, "start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configured REPL input does not exist", result.stderr)
        self.assertFalse((self.state / "pid").exists())

    def test_stop_does_not_signal_stale_pid(self):
        innocent = subprocess.Popen(["sleep", "30"])
        try:
            (self.state / "pid").write_text(json.dumps({
                "pid": innocent.pid, "signature": "former broker"
            }))
            stopped = self.run_command(REPL, "stop")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIsNone(innocent.poll())
        finally:
            innocent.terminate()
            innocent.wait(timeout=5)


@unittest.skipUnless(BROKER.exists(), "build the broker first")
class PersistentCommandTests(ConsumerCase):
    def test_lifecycle_preload_state_and_socket_ownership(self):
        (self.project / "preload.fsx").write_text("let initial = 19\n")
        (self.project / "fsrepl.json").write_text(json.dumps({"preload": "preload.fsx"}))
        pidfile = self.state / "pid"
        saved_pid = None
        try:
            started = self.run_command(REPL, "start", timeout=45)
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertIn("session up", started.stdout)
            self.assertEqual(self.run_command(REPL, "status").returncode, 0)
            self.assertEqual(self.run_command(REPL, "ping").returncode, 0)

            answer = self.run_command(REPL, "send", "initial + 1")
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertIn("20", answer.stdout)
            self.assertEqual(self.run_command(REPL, "send", "let later = initial + 2").returncode, 0)
            later = self.run_command(REPL, "send", "later")
            self.assertEqual(later.returncode, 0, later.stderr)
            self.assertIn("21", later.stdout)

            script_dir = self.project / "feature"
            script_dir.mkdir()
            (script_dir / "child.fsx").write_text("let answer = 22\n")
            feature = script_dir / "parent.fsx"
            feature.write_text('#load "child.fsx"\nlet derived = Child.answer + 1\n')
            loaded = self.run_command(REPL, "send", "--file", feature)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            derived = self.run_command(REPL, "send", "Parent.derived")
            self.assertEqual(derived.returncode, 0, derived.stderr)
            self.assertIn("23", derived.stdout)

            saved_pid = pidfile.read_text()
            pidfile.unlink()
            takeover = self.run_command(REPL, "start")
            self.assertNotEqual(takeover.returncode, 0)
            self.assertIn("broker still owns", takeover.stderr)
            self.assertEqual(self.run_command(REPL, "ping").returncode, 0)
            pidfile.write_text(saved_pid)
        finally:
            if saved_pid is not None and not pidfile.exists():
                pidfile.write_text(saved_pid)
            stopped = self.run_command(REPL, "stop")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)


class StatelessCommandTests(ConsumerCase):
    def check_script(self, args, expected, stdin=None, file_content=None):
        fake_dotnet = self.base / "dotnet"
        fake_dotnet.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            'pathlib.Path(os.environ["FSI_CAPTURE"]).write_text(json.dumps({'
            '"argv": sys.argv[1:], '
            '"source": pathlib.Path(sys.argv[-1]).read_bytes().decode("utf-8")}))\n'
        )
        fake_dotnet.chmod(0o755)
        if file_content is not None:
            source_file = self.project / "source file.fsx"
            source_file.write_bytes(file_content.encode("utf-8"))
            args = [str(source_file) if arg == "SOURCE_FILE" else arg for arg in args]
        capture = self.base / "capture.json"
        env = {
            **self.env,
            "PATH": f"{self.base}:{os.environ['PATH']}",
            "FSI_CAPTURE": str(capture),
        }
        result = self.run_command(*args, input=stdin, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(capture.read_text())
        self.assertEqual(actual["source"], expected)
        self.assertEqual(actual["argv"][:3], ["fsi", "--exec", "--quiet"])

    def test_expression_preserves_quotes(self):
        source = '"hello ; $HOME `id` café"'
        self.check_script([EVAL, source], f'printfn "%A" ({source})\n')

    def test_stdin_is_full_script(self):
        source = 'let answer = 19\r\nprintfn "%d" answer\r\n'
        self.check_script([EVAL, "-"], source, stdin=source)

    def test_file_runs_in_place(self):
        source = "module FeatureProbe\nlet answer = 19\n"
        self.check_script([EVAL, "--file", "SOURCE_FILE"], source, file_content=source)

    def test_build_argv_runs_in_consuming_project_and_preload_is_used(self):
        marker = self.project / "built.json"
        (self.project / "preload.fsx").write_text("let answer = 19\n")
        (self.project / "fsrepl.json").write_text(json.dumps({
            "preload": "preload.fsx",
            "build": ["python3", "-c",
                      f"import pathlib; pathlib.Path({str(marker)!r}).write_text('built')"],
        }))
        fake_dotnet = self.base / "dotnet"
        fake_dotnet.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            'pathlib.Path(os.environ["FSI_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n'
        )
        fake_dotnet.chmod(0o755)
        capture = self.base / "capture.json"
        env = {
            **self.env, "PATH": f"{self.base}:{os.environ['PATH']}",
            "FSI_CAPTURE": str(capture),
        }
        result = self.run_command(EVAL, "1 + 2", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), "built")
        argv = json.loads(capture.read_text())
        self.assertEqual(argv[:2], ["fsi", f"--use:{(self.project / 'preload.fsx').resolve()}"])

    def test_references_are_passed_to_stateless_fsi(self):
        assembly = self.project / "folder with spaces" / "Feature.dll"
        assembly.parent.mkdir()
        assembly.touch()
        (self.project / "fsrepl.json").write_text(json.dumps({
            "references": [str(assembly.relative_to(self.project))]
        }))
        fake_dotnet = self.base / "dotnet"
        fake_dotnet.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            'pathlib.Path(os.environ["FSI_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n'
        )
        fake_dotnet.chmod(0o755)
        capture = self.base / "capture.json"
        env = {**self.env, "PATH": f"{self.base}:{os.environ['PATH']}",
               "FSI_CAPTURE": str(capture)}
        result = self.run_command(EVAL, "1 + 2", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(capture.read_text())
        self.assertEqual(argv[:3], ["fsi", f"-r:{assembly.resolve()}", "--exec"])

    @unittest.skipUnless(BROKER.exists(), "build the broker first")
    def test_real_fsi_resolves_load_relative_to_file(self):
        script_dir = self.project / "feature"
        script_dir.mkdir()
        (script_dir / "feature.fsx").write_text("let answer = 19\n")
        main = script_dir / "main.fsx"
        main.write_text('#load "feature.fsx"\nprintfn "%d" Feature.answer\n')
        result = self.run_command(EVAL, "--file", main)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "19")


if __name__ == "__main__":
    unittest.main()
