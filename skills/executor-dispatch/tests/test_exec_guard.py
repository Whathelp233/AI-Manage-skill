"""Guard integration tests using local fake CLIs, never real executors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "exec_guard.py"
if not GUARD.is_file():
    GUARD = ROOT / "exec_guard.py"
FAKE = r'''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

args = sys.argv[1:]
root = Path(os.environ["CODEX_HOME"])
(root / "argv.json").write_text(json.dumps(args))
(root / "stdin.txt").write_text(sys.stdin.read())
if Path(sys.argv[0]).name == "claude":
    print("stderr forwarded", file=sys.stderr)
    count = 2 if "spawn" in args else 0
    result = {"type": "result", "subagent_stats": {"spawned": count},
              "modelUsage": {"fake-model": {}}}
    if "pretty" in args:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"type": "assistant"}))
        print(json.dumps(result))
    sys.exit(7)
sessions = root / "sessions/2026/11/13"
sessions.mkdir(parents=True, exist_ok=True)
def rollout(name, payload):
    (sessions / ("rollout-" + name + ".jsonl")).write_text(
        json.dumps({"payload": payload}) + "\n" +
        json.dumps({"payload": {"model": "child-model"}}) + "\n")
rollout("main", {"id": "main", "source": "exec"})
print("stdout forwarded", flush=True)
print("stderr forwarded", file=sys.stderr, flush=True)
if "spawn" in args or "foreign" in args or "quick" in args:
    rollout("child", {"id": "child", "thread_source": "subagent",
                      "parent_thread_id": "other" if "foreign" in args else "main",
                      "source": {"subagent": {"thread_spawn": {"depth": 1}}}})
if "stubborn" in args:
    subprocess.Popen([sys.executable, "-c",
        "import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(6);pathlib.Path(" + repr(str(root / "descendant_done")) +
        ").touch()"])
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(0 if "quick" in args else 6 if "spawn" in args else 1.2)
(root / "finished").touch()
sys.exit(7)
'''


class ExecGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("codex", "claude"):
            path = self.root / name
            path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE))
            path.chmod(0o755)
        self.env = {**os.environ, "CODEX_HOME": str(self.root),
                    "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}

    def invoke(self, executor, *args):
        report = self.root / "report.json"
        proc = subprocess.run(
            [sys.executable, str(GUARD), executor, "--report", str(report),
             "--", *args], env=self.env, input="input forwarded\n",
            capture_output=True, text=True, timeout=15)
        return proc, json.loads(report.read_text())

    def test_no_child_preserves_exit_code_and_streams(self):
        proc, report = self.invoke("codex")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual((self.root / "stdin.txt").read_text(), "input forwarded\n")
        self.assertIn("stdout forwarded", proc.stdout)
        self.assertIn("stderr forwarded", proc.stderr)
        self.assertTrue((self.root / "finished").exists())
        self.assertEqual(report["subagents_spawned"], 0)

    def test_child_terminates_before_sleep_finishes(self):
        proc, report = self.invoke("codex", "spawn")
        self.assertEqual(proc.returncode, 86, proc.stderr)
        self.assertFalse((self.root / "finished").exists())
        self.assertIn("parent=main, child=child, model=child-model", proc.stderr)
        self.assertEqual(report["subagents_spawned"], 1)
        self.assertEqual(report["subthreads"][0]["thread_id"], "child")
        self.assertEqual(report["models_used"], ["child-model"])

    def test_kills_stubborn_process_group(self):
        proc, _ = self.invoke("codex", "spawn", "stubborn")
        self.assertEqual(proc.returncode, 86, proc.stderr)
        self.assertFalse((self.root / "finished").exists())
        # An inherited stdout pipe would keep subprocess.run waiting until the
        # descendant completed if the guard only killed the leader.
        self.assertFalse((self.root / "descendant_done").exists())

    def test_foreign_child_does_not_kill_run(self):
        proc, report = self.invoke("codex", "foreign")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual(report["subagents_spawned"], 0)
        self.assertTrue((self.root / "finished").exists())

    def test_final_scan_detects_fast_child(self):
        proc, report = self.invoke("codex", "quick")
        self.assertEqual(proc.returncode, 86, proc.stderr)
        self.assertEqual(report["subagents_spawned"], 1)

    def test_preexisting_rollouts_are_ignored(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        for name, payload in (("old-main", {"id": "old", "source": "exec"}),
                              ("old-child", {"id": "old-child",
                               "thread_source": "subagent", "parent_thread_id": "old"})):
            (sessions / f"rollout-{name}.jsonl").write_text(
                json.dumps({"payload": payload}) + "\n")
        proc, report = self.invoke("codex")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual(report["subagents_spawned"], 0)

    def test_claude_inserts_comma_argument_before_prompt(self):
        proc, report = self.invoke("claude", "task instruction")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual(json.loads((self.root / "argv.json").read_text()),
                         ["-p", "--disallowedTools", "Agent,Task", "task instruction"])
        self.assertEqual(report["subagents_spawned"], 0)
        self.assertIn("stderr forwarded", proc.stderr)

    def test_claude_does_not_duplicate_explicit_option(self):
        for option in (["--disallowedTools", "Agent,Task"],
                       ["--disallowedTools=Agent,Task"]):
            proc, _ = self.invoke("claude", *option, "task")
            self.assertEqual(proc.returncode, 7, proc.stderr)
            self.assertEqual(json.loads((self.root / "argv.json").read_text()),
                             ["-p", *option, "task"])

    def test_claude_spawned_in_json_and_stream_json(self):
        for extra in ([], ["pretty"]):
            proc, report = self.invoke("claude", "spawn", *extra)
            self.assertEqual(proc.returncode, 86, proc.stderr)
            self.assertEqual(report["subagents_spawned"], 2)
            self.assertEqual(report["models_used"], ["fake-model"])
            self.assertIn('"type": "result"', proc.stdout)


if __name__ == "__main__":
    unittest.main()
