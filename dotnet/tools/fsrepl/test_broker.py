#!/usr/bin/env python3
"""Behavioural checks for the hosted FSI socket boundary.

Build the broker first. Each case uses a fresh consuming project and session.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest


TOOL_ROOT = Path(__file__).resolve().parents[3]
BROKER = TOOL_ROOT / "dotnet/tools/fsrepl/bin/Debug/net10.0/fsrepl.dll"


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fsrepl-test-", dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.project = self.base / "consumer"
        self.project.mkdir()
        self.socket_path = self.base / "session.sock"
        self.transcript_path = self.base / "transcript.ndjson"
        env = os.environ.copy()
        env.update(
            FSREPL_ROOT=str(self.project),
            FSREPL_SOCKET=str(self.socket_path),
            FSREPL_TRANSCRIPT=str(self.transcript_path),
        )
        self.process = subprocess.Popen(
            ["dotnet", str(BROKER)],
            cwd=self.project,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_broker)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(f"broker exited during startup: {self.process.stderr.read()}")
            if self.socket_path.exists():
                try:
                    if self.request("ping")["ok"]:
                        return
                except OSError:
                    pass
            time.sleep(0.05)
        self.fail("broker did not become ready within 20 seconds")

    def stop_broker(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stderr.close()

    def request(self, op, code="", chunk_at=None):
        payload = (
            json.dumps({"id": "test", "op": op, "code": code}, ensure_ascii=False) + "\n"
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(self.socket_path))
            if chunk_at is None:
                connection.sendall(payload)
            else:
                connection.sendall(payload[:chunk_at])
                connection.sendall(payload[chunk_at:])
            chunks = []
            while data := connection.recv(65536):
                chunks.append(data)
        return json.loads(b"".join(chunks))

    def test_one_line_load_uses_fsi_module_and_relative_load_semantics(self):
        scripts = self.base / "scripts"
        scripts.mkdir()
        (scripts / "child.fsx").write_text("module ChildProbe\nlet answer = 19\n")
        parent = scripts / "parent.fsx"
        parent.write_text('#load "child.fsx"\nlet derived = ChildProbe.answer + 1\n')

        loaded = subprocess.run(
            ["python3", str(TOOL_ROOT / "scripts/fsi-send.py"), str(self.socket_path),
             "eval", "--file", str(parent)],
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        derived = self.request("eval", "Parent.derived")
        self.assertTrue(derived["ok"], derived)
        self.assertEqual(derived["value"], "20")

    def test_utf8_can_split_across_socket_packets_and_response_is_complete(self):
        code = '"é"'
        payload = (
            json.dumps({"id": "test", "op": "eval", "code": code}, ensure_ascii=False)
            + "\n"
        ).encode()
        split = payload.index("é".encode()) + 1
        result = self.request("eval", code, chunk_at=split)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], '"é"')

        large = self.request("eval", 'printfn "%s" (String.replicate 150000 "é")')
        self.assertTrue(large["ok"], large)
        self.assertIn("é" * 150000, large["stdout"])

    def test_unknown_operation_is_rejected_without_evaluation(self):
        result = self.request("mistyped", "failwith \"should not run\"")
        self.assertFalse(result["ok"])
        self.assertIn("unknown operation", result["exception"])

    def test_invalid_utf8_returns_a_protocol_error(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(self.socket_path))
            connection.sendall(b'{"id":"x","op":"eval","code":"\xff"}\n')
            parts = []
            while data := connection.recv(65536):
                parts.append(data)
        result = json.loads(b"".join(parts))
        self.assertFalse(result["ok"])
        self.assertIn("bad request", result["exception"])

    def test_evaluated_stderr_reaches_response_and_transcript(self):
        for source, expected in [
            ('eprintfn "fsi stderr"', "fsi stderr"),
            ('System.Console.Error.WriteLine("console stderr")', "console stderr"),
        ]:
            with self.subTest(source=source):
                result = self.request("eval", source)
                self.assertTrue(result["ok"], result)
                self.assertIn(expected, result["stderr"])
                events = [json.loads(line) for line in self.transcript_path.read_text().splitlines()]
                self.assertIn(expected, events[-1]["stderr"])

    def test_invalid_preload_never_advertises_readiness(self):
        fake_root = self.base / "bad-root"
        fake_root.mkdir()
        (fake_root / "fsrepl.json").write_text(json.dumps({"preload": "broken.fsx"}))
        (fake_root / "broken.fsx").write_text("let broken = missingSymbol\n")
        bad_socket = self.base / "bad.sock"
        env = os.environ.copy()
        env.update(
            FSREPL_ROOT=str(fake_root),
            FSREPL_SOCKET=str(bad_socket),
            FSREPL_TRANSCRIPT=str(self.base / "bad-transcript.ndjson"),
        )
        failed = subprocess.run(
            ["dotnet", str(BROKER)],
            cwd=fake_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("preloaded session failed", failed.stderr)
        self.assertFalse(bad_socket.exists())

    def test_second_broker_cannot_unlink_a_live_socket(self):
        env = os.environ.copy()
        env.update(
            FSREPL_ROOT=str(self.project),
            FSREPL_SOCKET=str(self.socket_path),
            FSREPL_TRANSCRIPT=str(self.base / "second-transcript.ndjson"),
        )
        second = subprocess.run(
            ["dotnet", str(BROKER)], cwd=self.project, env=env,
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertTrue(self.request("ping")["ok"])


if __name__ == "__main__":
    unittest.main()
