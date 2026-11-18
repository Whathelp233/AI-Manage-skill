"""tools/bench.py 的单测。

全部用临时目录里的夹具题（Python 小题，不依赖 g++）和假执行器
tests/fake_executor.py，不调用真实的 codex / claude / dsh-exec，也不依赖仓库里
config/models.yaml 和 bench/tasks 的具体内容。
"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
MODULE_DIR = TESTS_DIR.parent
if not (MODULE_DIR / "bench.py").is_file():
    MODULE_DIR = MODULE_DIR / "tools"
sys.path.insert(0, str(MODULE_DIR))
import bench  # noqa: E402

FAKE = TESTS_DIR / "fake_executor.py"
COMMON = TESTS_DIR.parent / "common"
if not COMMON.is_dir():
    COMMON = TESTS_DIR.parent / "bench" / "common"
sys.path.insert(0, str(COMMON))
import py_score  # noqa: E402

TASK_MD = """\
# 夹具任务 {task_id}

实现 `src/calc.py` 里的 `add(a, b)`。

- 为改动编写测试，放在 test/ 下。
- 在工作区根目录写 REPORT.md，第一行是 `TESTS: PASS`、`TESTS: FAIL` 或
  `TESTS: NOT_RUN`。
"""

STARTER_TEST = """\
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import calc

failures = 0


def check(condition, what):
    global failures
    if not condition:
        failures += 1
        print("FAIL", what)


check(calc.add(0, 0) == 0, "0 + 0")
sys.exit(1 if failures else 0)
"""

REFERENCE_TEST = STARTER_TEST.replace(
    'check(calc.add(0, 0) == 0, "0 + 0")\n',
    'check(calc.add(0, 0) == 0, "0 + 0")\n'
    'check(calc.add(1, 2) == 3, "1 + 2")\n'
    'check(calc.add(-4, 1) == -3, "-4 + 1")\n')

SELF_TEST = """\
import glob
import json
import subprocess
import sys

tests = sorted(glob.glob("test/test_*.py"))
passed = 0
for path in tests:
    proc = subprocess.run([sys.executable, path])
    passed += 1 if proc.returncode == 0 else 0
print(json.dumps({"compile_ok": True, "passed": passed, "total": len(tests)}))
"""

SCORE = """\
import json
import sys

GROUPS = {groups}
CHECKS = {checks}
sys.path.insert(0, "src")
result = {{name: [0, total] for name, total in GROUPS.items()}}
compile_ok = True
try:
    import calc
except Exception:
    compile_ok = False
if compile_ok:
    for group, a, b in CHECKS:
        try:
            if calc.add(a, b) == a + b:
                result[group][0] += 1
        except Exception:
            pass
print("some log line")
print(json.dumps({{
    "passed": sum(p for p, _ in result.values()),
    "total": sum(t for _, t in result.values()),
    "groups": result, "compile_ok": compile_ok}}))
"""

CONFIG = """\
pool: []
routing: {}
rework: {}
thresholds:
  median_time_ratio: 0.7
  cost_ratio: 0.5
"""


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_task(bench_dir, task_id, category, split):
    """在夹具 bench 目录里建一道 Python 小题。"""
    task = Path(bench_dir) / "tasks" / task_id
    if category == "C":
        groups = {"basic": 2, "safety": 1, "asan": 1, "tsan": 1}
        checks = [("basic", 1, 2), ("basic", -1, 1), ("safety", 10, 5),
                  ("asan", 2, 2), ("tsan", 3, 3)]
    else:
        groups = {"basic": 2, "edge": 1}
        checks = [("basic", 1, 2), ("basic", -1, 1), ("edge", 10, 5)]
    meta = {
        "id": task_id, "category": category, "split": split,
        "language": "python", "score_cmd": "python3 hidden/score.py",
        "timeout_s": 60, "total_checks": sum(groups.values()),
        "groups": list(groups), "summary": "夹具题",
        "allowed_paths": ["src/**", "test/**", "REPORT.md"],
        "self_test_cmd": 'python3 "$BENCH_COMMON/fx_self_test.py"',
        "starter_tests": ["test/test_calc.py"],
    }
    write(task / "meta.yaml", json.dumps(meta, ensure_ascii=False))
    write(task / "task.md", TASK_MD.format(task_id=task_id))
    write(task / "starter/src/calc.py", "def add(a, b):\n    return 0\n")
    write(task / "starter/test/test_calc.py", STARTER_TEST)
    write(task / "reference/src/calc.py",
          "def add(a, b):\n    return a + b\n")
    write(task / "reference/test/test_calc.py", REFERENCE_TEST)
    write(task / "reference/REPORT.md", "TESTS: PASS\n\n实现了 add。\n")
    write(task / "hidden/score.py",
          SCORE.format(groups=repr(groups), checks=repr(checks)))
    write(task / "hidden/secret_marker.txt", "hidden test material\n")
    return task


def record(model, split="holdout", category="A", pass_rate=1.0, wall_s=10.0,
           cost_usd=None, honest=True, safety_ok=None, task="t1", rep=0,
           run_id="r1", executor_status="ok", executor="codex",
           tokens_in=100, tokens_out=20):
    """judge / report 测试用的合成结果记录。"""
    return {
        "run_id": run_id, "task": task, "category": category,
        "split": split, "model": model, "executor": executor, "rep": rep,
        "executor_md": "none", "executor_md_sha256": None,
        "executor_status": executor_status, "wall_s": wall_s,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "pass_rate": pass_rate, "safety_ok": safety_ok, "honest": honest,
        "tests_written": True, "scope_violations": [],
        "report_claim": "PASS",
        "style": {"passed": 11, "total": 11, "ratio": 1.0, "failed": []},
    }


class BenchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bench = self.tmp / "bench"
        write(self.bench / "common/fx_self_test.py", SELF_TEST)
        write(self.bench / "TASKS.md", "不得放入被测模型工作区\n")
        make_task(self.bench, "fx_a_dev", "A", "dev")
        make_task(self.bench, "fx_c_hold", "C", "holdout")
        self.config = self.tmp / "models.yaml"
        write(self.config, CONFIG)
        self.bwrap = self.tmp / "bwrap"
        write(self.bwrap, "#!/usr/bin/env python3\nimport os, sys\n"
              "i = sys.argv.index('--')\nos.execvp(sys.argv[i+1], sys.argv[i+1:])\n")
        self.bwrap.chmod(0o755)
        self.seen = self.tmp / "seen"
        self.executor_md = self.tmp / "EXECUTOR.md"
        write(self.executor_md, "测试执行器说明 v1\n")

    def _auth_home(self):
        path = self.tmp / "codex_home"
        path.mkdir(exist_ok=True)
        (path / "auth.json").touch()
        return str(path)

    def call(self, *argv):
        """调用 bench.main，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--bench-dir", str(self.bench)] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def fake_cmd(self, mode, emit):
        return (f"{sys.executable} {FAKE} --mode {mode} --emit {emit} "
                f"--ws {{ws}} --task {{task}} --bench {self.bench} "
                f"--model {{model}} --prompt {{prompt}} "
                f"--prompt-file {{prompt_file}} --seen-dir {self.seen}")

    def run_fake(self, mode, emit="codex", executor="codex", model="m-test",
                 run_id="run1", extra=()):
        codex_home = self.tmp / "codex_home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "auth.json").touch()
        argv = ["run", "--model", model, "--executor", executor,
                "--reps", "1", "--run-id", run_id,
                "--executor-cmd", self.fake_cmd(mode, emit),
                "--codex-home", str(codex_home),
                "--executor-md", str(self.executor_md),
                "--bwrap", str(self.bwrap)] + list(extra)
        if "--work-root" not in extra:
            argv += ["--work-root", str(self.tmp / "work")]
        if "--tasks" not in extra and "--split" not in extra:
            argv += ["--tasks", "fx_a_dev"]
        return self.call(*argv)

    def results(self, run_id="run1"):
        path = self.bench / "results" / run_id / "results.jsonl"
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()]

    def write_results(self, run_id, records):
        path = self.bench / "results" / run_id / "results.jsonl"
        write(path, "".join(json.dumps(r) + "\n" for r in records))

    def judge(self, *run_ids, candidate="cand", incumbent="inc",
              category="A"):
        code, out, err = self.call(
            "judge", "--candidate", candidate, "--incumbent", incumbent,
            "--category", category, "--config", str(self.config), *run_ids)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class SelfcheckTest(BenchTestCase):
    def test_python_source_line_count_excludes_test_directory(self):
        source = self.tmp / "python_lines"
        write(source / "src/module.py", "one\ntwo\n")
        write(source / "test/test_module.py", "ignored\nignored\nignored\n")
        write(source / "helper.py", "three\n")
        self.assertEqual(bench.count_source_lines(source, "python"), 3)

    def test_python_regression_filename(self):
        self.assertEqual(bench.REGRESSION_TESTS["python"],
                         "test_regression.py")

    def test_mask_argv_and_directories(self):
        extra = self.tmp / "extra"
        extra.mkdir()
        paths = bench.mask_directories(self.bench, [str(extra),
                                                    str(self.tmp / "missing")])
        self.assertIn(str(bench.ROOT.resolve()), paths)
        self.assertIn(str(extra), paths)
        self.assertNotIn(str(self.tmp / "missing"), paths)
        argv = bench.mask_argv(["executor", "arg"], "/tmp/ws", [str(extra)],
                               "/fake/bwrap")
        self.assertEqual(argv, ["/fake/bwrap", "--bind", "/", "/", "--dev",
                                "/dev", "--proc", "/proc", "--tmpfs",
                                str(extra), "--chdir", "/tmp/ws", "--",
                                "executor", "arg"])

    def test_mask_dirs_from_environment(self):
        extra = self.tmp / "env-mask"
        extra.mkdir()
        with mock.patch.dict(bench.os.environ,
                             {"BENCH_MASK_DIRS": str(extra)}):
            paths = bench.mask_directories(self.bench, [])
        self.assertIn(str(extra), paths)
        self.assertEqual(len(paths), 2)

    def test_bench_env_loads_only_missing_bench_keys(self):
        config = self.tmp / "bench.env"
        config.write_text("BENCH_PYTHON=/configured/python\nOTHER=x\n")
        with mock.patch.dict(bench.os.environ,
                             {"BENCH_PYTHON": "/environment/python"},
                             clear=True):
            bench.load_bench_env(config)
            self.assertEqual(bench.os.environ["BENCH_PYTHON"],
                             "/environment/python")
            self.assertNotIn("OTHER", bench.os.environ)

    def test_score_subprocess_inherits_bench_python(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (
            '{"passed": 0, "total": 0, "groups": {}, '
            '"compile_ok": true}', None)
        with mock.patch.dict(bench.os.environ,
                             {"BENCH_PYTHON": "/configured/python"}):
            with mock.patch.object(bench.subprocess, "Popen",
                                   return_value=process) as popen:
                bench.run_score({"score_cmd": "score", "timeout_s": 1},
                                self.tmp)
                self.assertEqual(popen.call_args.kwargs["env"]["BENCH_PYTHON"],
                                 "/configured/python")

    def test_python_score_interpreter_override_and_fallback(self):
        with mock.patch.dict(bench.os.environ,
                             {"BENCH_PYTHON": "/custom/python"}):
            self.assertEqual(py_score.default_python(), "/custom/python")
        with mock.patch.dict(bench.os.environ, {}, clear=True):
            self.assertEqual(py_score.default_python(), sys.executable)

    def test_mask_overlap_rejected(self):
        for label in ("workspace", "CODEX_HOME", "work root", "executor-md"):
            with self.subTest(label=label), self.assertRaisesRegex(
                    bench.BenchError, label):
                bench.validate_mask_dirs([str(self.tmp)],
                                         [(label, self.tmp / "child")])

    def test_mask_records_and_executor_md_copy(self):
        code, _, err = self.run_fake("noop", executor="claude", emit="claude")
        self.assertEqual(code, 0, err)
        (record,) = self.results()
        self.assertIn(str(bench.ROOT.resolve()), record["mask_dirs"])
        copied = self.tmp / "work" / "run1" / "EXECUTOR.md"
        self.assertEqual(copied.read_text(), self.executor_md.read_text())
        code, out, err = self.call("report", "run1")
        self.assertEqual(code, 0, err)
        self.assertIn("masked", out)

    def test_mask_probe_failure_dev_and_holdout(self):
        code, _, err = self.run_fake("noop", extra=("--bwrap", "/missing/bwrap"))
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)
        self.assertIsNone(self.results()[0]["mask_dirs"])
        code, _, err = self.run_fake("noop", run_id="hold", extra=(
            "--tasks", "fx_c_hold", "--confirm-holdout", "--bwrap",
            "/missing/bwrap"))
        self.assertEqual(code, 2)
        self.assertIn("bwrap", err)
        code, _, err = self.run_fake("noop", run_id="hold", extra=(
            "--tasks", "fx_c_hold", "--confirm-holdout", "--no-mask",
            "--bwrap", "/missing/bwrap"))
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.results("hold")[0]["mask_dirs"])

    def test_leak_paths_match_stdout(self):
        hints = bench.find_leak_hints(
            "executor ran /workspace/private/src/secret.py",
            ["private/src/secret.py"])
        self.assertEqual(len(hints), 1)
        self.assertTrue(hints[0].startswith("private/src/secret.py: ..."))

    def test_leak_paths_validation(self):
        path = self.bench / "tasks/fx_a_dev/meta.yaml"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["leak_paths"] = ["", "src/private.py"]
        write(path, json.dumps(meta))
        with self.assertRaisesRegex(bench.BenchError, "leak_paths"):
            bench.load_meta(path.parent)

    def test_common_scripts_found_in_both_layouts(self):
        self.assertTrue((COMMON / "mini_test.h").is_file())
        self.assertTrue((COMMON / "score_lib.sh").is_file())
        proc = subprocess.run(["bash", str(COMMON / "self_test.sh")],
                              cwd=self.tmp, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[-1]),
                         {"compile_ok": False, "passed": 0, "total": 1})

    def test_cli_defaults_match_install_layout(self):
        parser = bench.build_parser()
        args = parser.parse_args(["run", "--model", "fake", "--executor", "codex"])
        self.assertEqual(Path(args.bench_dir), bench.ROOT / "bench")
        self.assertEqual(Path(args.executor_md), bench.DEFAULT_EXECUTOR_MD)
        self.assertTrue(Path(args.executor_md).is_file())
        args = parser.parse_args([
            "--bench-dir", str(self.bench), "judge", "--candidate", "new",
            "--incumbent", "old", "--category", "A", "r"])
        self.assertEqual(Path(args.config), bench.DEFAULT_CONFIG)

    def test_fixture_tasks_pass(self):
        code, out, err = self.call("selfcheck", "--tmp", str(self.tmp))
        self.assertEqual(code, 0, out + err)
        self.assertIn("selfcheck: 2/2 题通过", out)
        self.assertIn("[OK  ] fx_a_dev", out)

    def test_reference_not_full_score_fails(self):
        write(self.bench / "tasks/fx_a_dev/reference/src/calc.py",
              "def add(a, b):\n    return a - b\n")
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("reference 不是满分", out)
        self.assertIn("reference 的 self_test 没通过", out)

    def test_starter_full_score_fails(self):
        write(self.bench / "tasks/fx_a_dev/starter/src/calc.py",
              "def add(a, b):\n    return a + b\n")
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("区分度不足", out)

    def test_reference_report_must_claim_pass(self):
        write(self.bench / "tasks/fx_a_dev/reference/REPORT.md",
              "TESTS: NOT_RUN\n")
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("第一行必须是 TESTS: PASS", out)

    def test_reference_scope_violation_fails(self):
        write(self.bench / "tasks/fx_a_dev/reference/extra.txt", "x\n")
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("reference 违反 scope 规则", out)

    def test_meta_with_unknown_field_fails(self):
        path = self.bench / "tasks/fx_a_dev/meta.yaml"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["surprise"] = 1
        write(path, json.dumps(meta))
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("字段不符", out)

    def test_task_md_leaking_hidden_fails(self):
        path = self.bench / "tasks/fx_a_dev/task.md"
        write(path, path.read_text(encoding="utf-8") + "\n见 hidden/ 目录\n")
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_a_dev")
        self.assertEqual(code, 1)
        self.assertIn("task.md 提到了 hidden/", out)

    def test_c_task_requires_safety_groups(self):
        make_task(self.bench, "fx_c_bad", "A", "dev")
        path = self.bench / "tasks/fx_c_bad/meta.yaml"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["category"] = "C"
        write(path, json.dumps(meta))
        code, out, _ = self.call("selfcheck", "--tmp", str(self.tmp),
                                 "--tasks", "fx_c_bad")
        self.assertEqual(code, 1)
        self.assertIn("C 类必须有 safety 组", out)


class RunTest(BenchTestCase):
    def config_effort(self, effort):
        config = bench.yaml.safe_load(self.config.read_text(encoding="utf-8"))
        config["pool"] = [{
            "id": "m-test", "executor": "codex", "effort": effort,
        }]
        write(self.config, bench.yaml.safe_dump(config, allow_unicode=True))

    def test_none_baseline_records_null_hash_and_uses_codex_home(self):
        code, _, err = self.run_fake(
            "noop", extra=("--executor-md", "none"))
        self.assertEqual(code, 0, err)
        rec = self.results()[0]
        self.assertEqual(rec["executor_md"], "none")
        self.assertIsNone(rec["executor_md_sha256"])
        self.assertEqual(rec["isolation"], str(self.tmp / "codex_home"))

    def test_codex_home_is_pinned_with_md_copy(self):
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        home = self.tmp / "work/run1/_codex_home"
        self.assertEqual((home / "AGENTS.md").read_text(),
                         "测试执行器说明 v1\n")
        self.assertEqual((home / "config.toml").read_text(),
                         'model_reasoning_effort = "medium"\n')
        self.assertEqual(json.loads((self.bench / "results/run1/run.json")
                                    .read_text())['effort'], 'medium')
        manifest = json.loads((self.bench / "results/run1/run.json")
                              .read_text())
        self.assertEqual(manifest["effort_source"], "default")
        self.assertEqual(self.results()[0]['effort'], 'medium')
        self.assertTrue((home / "auth.json").is_symlink())
        self.assertEqual((home / "auth.json").resolve(),
                         (self.tmp / "codex_home/auth.json").resolve())

    def test_effort_high_used_in_codex_command_and_config(self):
        code, _, err = self.run_fake("noop", extra=("--effort", "high"))
        self.assertEqual(code, 0, err)
        home = self.tmp / "work/run1/_codex_home"
        self.assertIn('model_reasoning_effort = "high"',
                      (home / "config.toml").read_text())
        manifest = json.loads((self.bench / "results/run1/run.json")
                              .read_text())
        self.assertEqual(manifest["effort"], "high")
        self.assertEqual(manifest["effort_source"], "cli")
        self.assertEqual(self.results()[0]["effort"], "high")
        argv = bench.build_executor_argv(
            bench.EXECUTOR_TEMPLATES['codex'],
            {'ws': '/w', 'model': 'm', 'effort': 'high', 'last': '/l',
             'prompt': 'p'})
        self.assertIn('model_reasoning_effort=high', argv)

    def test_effort_defaults_to_model_config(self):
        self.config_effort("xhigh")
        code, _, err = self.run_fake(
            "noop", extra=("--config", str(self.config)))
        self.assertEqual(code, 0, err)
        manifest = json.loads((self.bench / "results/run1/run.json")
                              .read_text())
        self.assertEqual(manifest["effort"], "xhigh")
        self.assertEqual(manifest["effort_source"], "config")
        self.assertIn('model_reasoning_effort = "xhigh"',
                      (self.tmp / "work/run1/_codex_home/config.toml")
                      .read_text())

    def test_cli_effort_overrides_model_config(self):
        self.config_effort("max")
        code, _, err = self.run_fake(
            "noop", extra=("--config", str(self.config),
                           "--effort", "low"))
        self.assertEqual(code, 0, err)
        manifest = json.loads((self.bench / "results/run1/run.json")
                              .read_text())
        self.assertEqual(manifest["effort"], "low")
        self.assertEqual(manifest["effort_source"], "cli")
        self.assertEqual(self.results()[0]["effort"], "low")

    def test_missing_model_config_defaults_to_medium(self):
        missing_config = self.tmp / "missing-models.yaml"
        code, _, err = self.run_fake(
            "noop", extra=("--config", str(missing_config)))
        self.assertEqual(code, 0, err)
        manifest = json.loads((self.bench / "results/run1/run.json")
                              .read_text())
        self.assertEqual(manifest["effort"], "medium")
        self.assertEqual(manifest["effort_source"], "default")

    def test_claude_rejects_explicit_effort(self):
        code, _, err = self.run_fake("noop", executor="claude",
                                     extra=("--effort", "high"))
        self.assertNotEqual(code, 0)
        self.assertIn("只支持 codex", err)

    def test_custom_executor_command_without_effort_placeholder_works(self):
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)

    def test_claude_appends_instruction_file_and_exclusion_settings(self):
        rep_dir = self.tmp / "claude_rep"
        rep_dir.mkdir()
        (rep_dir / "executor.stdout").write_text(
            '{"type":"result","result":"ok"}\n')
        captured = {}
        class Proc:
            def wait(self, timeout=None):
                return 0
        def popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return Proc()
        prompt_file = rep_dir / "prompt"
        prompt_file.write_text("task")
        with mock.patch.object(bench.subprocess, "Popen", side_effect=popen):
            bench.run_executor("claude", "claude -p {prompt}", {
                "ws": str(rep_dir), "model": "m", "prompt": "task",
                "prompt_file": str(prompt_file)}, rep_dir, 1,
                executor_md=str(self.executor_md.resolve()))
        argv = captured["argv"]
        self.assertEqual(argv[-2], "--settings")
        settings = json.loads(argv[-1])
        self.assertEqual(settings["claudeMdExcludes"], [
            str(Path.home() / ".claude/CLAUDE.md")])
        self.assertEqual(argv[-4:-2], ["--append-system-prompt-file",
                                      str(self.executor_md.resolve())])

    def test_missing_executor_md_fails(self):
        code, _, err = self.run_fake(
            "noop", extra=("--executor-md", str(self.tmp / "missing.md")))
        self.assertEqual(code, 2)
        self.assertIn("executor 说明文件不存在", err)

    def test_reference_executor_gets_full_score(self):
        code, out, err = self.run_fake("reference")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["task"], "fx_a_dev")
        self.assertEqual(rec["category"], "A")
        self.assertEqual(rec["split"], "dev")
        self.assertEqual(rec["model"], "m-test")
        self.assertEqual(rec["executor"], "codex")
        self.assertEqual(rec["isolation"],
                         str(self.tmp / "work/run1/_codex_home"))
        self.assertTrue(rec["executor_md"].endswith("EXECUTOR.md"))
        self.assertEqual(len(rec["executor_md_sha256"]), 12)
        self.assertEqual(rec["exit_code"], 0)
        self.assertEqual(rec["executor_status"], "ok")
        self.assertTrue(rec["executor_ok"])
        self.assertEqual(rec["score"]["passed"], rec["score"]["total"])
        self.assertEqual(rec["pass_rate"], 1.0)
        self.assertIsNone(rec["score_error"])
        self.assertIsInstance(rec["wall_s"], float)
        self.assertEqual((rec["tokens_in"], rec["tokens_out"]), (100, 30))
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(rec["model_actual"], "m-test")
        self.assertTrue(rec["tests_written"])
        self.assertEqual(rec["scope_violations"], [])
        self.assertEqual(rec["report_claim"], "PASS")
        self.assertEqual(rec["self_test"],
                         {"compile_ok": True, "passed": 1, "total": 1})
        self.assertIs(rec["honest"], True)
        self.assertIn("ratio", rec["style"])
        self.assertIsNone(rec["safety_ok"])

    def test_noop_executor_scores_like_starter(self):
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["score"]["passed"], 1)
        self.assertEqual(rec["score"]["total"], 3)
        self.assertFalse(rec["tests_written"])
        self.assertEqual(rec["scope_violations"], [])
        self.assertEqual(rec["report_claim"], "missing")
        self.assertIsNone(rec["honest"])

    def test_reps_and_multiple_tasks(self):
        code, out, err = self.call(
            "run", "--model", "m-test", "--executor", "codex", "--reps", "2",
            "--run-id", "multi", "--confirm-holdout", "--bwrap", str(self.bwrap),
            "--executor-cmd", self.fake_cmd("reference", "codex"),
            "--work-root", str(self.tmp / "work"))
        self.assertEqual(code, 0, err)
        recs = self.results("multi")
        self.assertEqual([(r["task"], r["rep"]) for r in recs],
                         [("fx_a_dev", 0), ("fx_a_dev", 1),
                          ("fx_c_hold", 0), ("fx_c_hold", 1)])
        self.assertTrue(all(r["pass_rate"] == 1.0 for r in recs))
        self.assertIs(recs[2]["safety_ok"], True)
        for task, rep in (("fx_a_dev", 0), ("fx_c_hold", 1)):
            ws = self.tmp / "work/multi" / task / "m-test" / str(rep) / "ws"
            self.assertTrue((ws / "src/calc.py").is_file())

    def test_claude_output_parsing(self):
        code, _, err = self.run_fake("reference", emit="claude",
                                     executor="claude", model="haiku",
                                     extra=("--executor-md", "none"))
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["tokens_in"], 100)
        self.assertEqual(rec["tokens_out"], 30)
        self.assertAlmostEqual(rec["cost_usd"], 0.0123)
        self.assertEqual(rec["model"], "haiku")
        self.assertEqual(rec["model_actual"], "claude-haiku-5-5")
        self.assertEqual(rec["isolation"],
                         "setting-sources=project;outside-home")
        # claude 的 cwd 是工作区，stdin 接 /dev/null。
        (seen,) = [json.loads(p.read_text(encoding="utf-8"))
                   for p in self.seen.iterdir()]
        ws = self.tmp / "work/run1/fx_a_dev/haiku/0/ws"
        self.assertEqual(Path(seen["cwd"]).resolve(), ws.resolve())
        self.assertEqual(seen["stdin"], "")

    def test_delegation_from_fake_traces(self):
        cases = [
            ("claude", "claude", "haiku", False),
            ("claude_agent", "claude", "haiku", True),
            ("claude_task", "claude", "haiku", True),
            ("claude_bash", "claude", "haiku", True),
            ("claude_multi", "claude", "haiku", True),
            ("codex_delegate", "codex", "m-test", True),
            ("codex_unknown", "codex", "m-test", None),
            ("codex_collab", "codex", "m-test", True),
            ("dsh_delegate", "dsh", "deepseek-flash", True),
        ]
        for index, (emit, executor, model, expected) in enumerate(cases):
            with self.subTest(emit=emit):
                run_id = f"delegation{index}"
                code, _, err = self.run_fake(
                    "reference", emit=emit, executor=executor, model=model,
                    run_id=run_id, extra=("--executor-md", "none"))
                self.assertEqual(code, 0, err)
                rec = self.results(run_id)[0]
                self.assertIs(rec["delegation_detected"], expected)
                if executor == "claude":
                    self.assertEqual(rec["subagents_spawned"], 0)
                    self.assertIn("claude-haiku-5-5", rec["models_used"])
                elif executor == "codex":
                    self.assertEqual(rec["subagents_spawned"], 0)
                else:
                    self.assertIsNone(rec["subagents_spawned"])
                if emit == "codex_unknown":
                    self.assertEqual(rec["unknown_item_types"], ["future_item"])
                if emit == "claude_bash":
                    self.assertEqual(rec["delegation_commands"], ["codex exec 'solve'"])
                if emit == "claude":
                    self.assertEqual(rec["delegation_commands"], [])
                    legacy = {"type": "result", "usage": {
                        "input_tokens": 10, "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 85, "output_tokens": 30},
                        "total_cost_usd": 0.0123,
                        "modelUsage": {"claude-haiku-5-5": {"outputTokens": 30}}}
                    parsed = bench.parse_executor_output(
                        "claude", "haiku", 0, json.dumps(legacy, indent=2))
                    for key in ("cost_usd", "tokens_in", "tokens_out", "model_actual"):
                        self.assertEqual(rec[key], parsed[key])

    def test_claude_default_blocks_subagents_and_streams(self):
        template = bench.EXECUTOR_TEMPLATES["claude"]
        self.assertIn("--disallowedTools Agent,Task", template)
        self.assertIn("--output-format stream-json --verbose", template)

    def test_codex_rollouts_recorded_without_rejecting_execution(self):
        code, _, err = self.run_fake(
            "reference", emit="codex_rollouts",
            extra=("--reps", "2"))
        self.assertEqual(code, 0, err)
        records = self.results()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["subagents_spawned"], 1)
            self.assertEqual(set(record["models_used"]), {"m-test", "child-model"})
            self.assertIs(record["delegation_detected"], True)
            self.assertTrue(record["executor_ok"])
            self.assertEqual(record["exit_code"], 0)

    def test_codex_rollouts_belong_to_parent_record(self):
        sessions = self.tmp / "shared_home" / "sessions" / "2026/11/13"
        sessions.mkdir(parents=True)
        for child, parent, model in (("a", "parent-a", "model-a"),
                                     ("b", "parent-b", "model-b"),
                                     ("c", "parent-a", "model-c")):
            payload = {"id": child, "thread_source": "subagent",
                       "parent_thread_id": parent}
            (sessions / f"rollout-{child}.jsonl").write_text(
                json.dumps({"payload": payload}) + "\n" + json.dumps({
                    "payload": {"model": model}}) + "\n", encoding="utf-8")
        for parent, count, models in (("parent-a", 2, ["model-a", "model-c"]),
                                      ("parent-b", 1, ["model-b"]),
                                      ("parent-other", 0, [])):
            stdout = "\n".join(map(json.dumps, [
                {"type": "thread.started", "thread_id": parent},
                {"type": "turn.completed"}]))
            parsed = bench.parse_executor_output(
                "codex", "requested", 0, stdout, sessions.parent.parent.parent)
            self.assertEqual(parsed["subagents_spawned"], count)
            self.assertEqual(set(parsed["models_used"]), {"requested", *models})
            self.assertIs(parsed["delegation_detected"], bool(count))
            self.assertTrue(parsed["executor_ok"])

    def test_delegation_limits_and_missing_data(self):
        events = [{"type": "item.completed", "item": {
            "type": "command_execution", "command": f"/opt/bin/dsh-exec ws {i}"}}
            for i in range(12)]
        parsed = bench.parse_executor_output(
            "codex", "m", 0, "\n".join(map(json.dumps, events)))
        self.assertIs(parsed["delegation_detected"], True)
        self.assertEqual(len(parsed["delegation_commands"]), 10)
        for executor in ("codex", "claude", "dsh"):
            model = "deepseek-flash" if executor == "dsh" else "m"
            parsed = bench.parse_executor_output(executor, model, 0, "garbage")
            self.assertIsNone(parsed["delegation_detected"])
        event = {"type": "result", "modelUsage": {"claude-haiku-5-5": {}},
                 "subagent_stats": {"spawned": 2}}
        parsed = bench.parse_executor_output("claude", "haiku", 0, json.dumps(event))
        self.assertIs(parsed["delegation_detected"], True)
        self.assertEqual(parsed["subagents_spawned"], 2)

    def test_dsh_branch(self):
        code, _, err = self.run_fake("reference", emit="dsh", executor="dsh",
                                     model="deepseek-flash")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["executor_status"], "ok")
        self.assertTrue(rec["executor_ok"])
        self.assertEqual(rec["model_actual"], "deepseek-flash")
        self.assertIsNone(rec["isolation"])
        self.assertEqual((rec["tokens_in"], rec["tokens_out"]), (70, 9))
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(rec["pass_rate"], 1.0)
        rep_dir = Path(rec["workspace"]).parent
        # 任务文件在工作区外，stdout 存成 dsh.jsonl，stdin 就是任务文件。
        self.assertTrue((rep_dir / "task.txt").is_file())
        self.assertFalse((rep_dir / "ws/task.txt").exists())
        self.assertIn("turn_end",
                      (rep_dir / "dsh.jsonl").read_text(encoding="utf-8"))
        (seen,) = [json.loads(p.read_text(encoding="utf-8"))
                   for p in self.seen.iterdir()]
        task_md = (self.bench / "tasks/fx_a_dev/task.md").read_text(
            encoding="utf-8")
        self.assertEqual(seen["stdin"],
                         "测试执行器说明 v1\n\n\n---\n\n" + task_md)

    def test_dsh_needs_completed_turn_end(self):
        # 退出码 0 且有 final 事件，但 turn_end 不是 completed：不算成功。
        code, _, err = self.run_fake("reference", emit="dsh_incomplete",
                                     executor="dsh", model="deepseek-flash")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["exit_code"], 0)
        self.assertFalse(rec["executor_ok"])
        self.assertEqual(rec["executor_status"], "error")
        self.assertIsNone(rec["tokens_in"])
        self.assertEqual(rec["pass_rate"], 1.0)

    def test_dsh_default_template_reads_stdin(self):
        self.assertEqual(
            bench.build_executor_argv(bench.EXECUTOR_TEMPLATES["dsh"],
                                      {"ws": "/w"})[1:],
            ["/w", "--json", "-"])

    def test_prompt_is_task_md_as_single_argument(self):
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        (seen,) = [json.loads(p.read_text(encoding="utf-8"))
                   for p in self.seen.iterdir()]
        task_md = (self.bench / "tasks/fx_a_dev/task.md").read_text(
            encoding="utf-8")
        self.assertEqual(seen["prompt"], task_md)
        self.assertEqual(seen["model"], "m-test")

    def test_default_templates_match_documented_commands(self):
        values = {"ws": "/w", "model": "m", "prompt": "do it\nnow",
                  "last": "/w/../last.md", "effort": "medium"}
        codex = bench.build_executor_argv(
            bench.EXECUTOR_TEMPLATES["codex"], values)
        self.assertEqual(codex, [
            "codex", "exec", "-C", "/w", "-m", "m", "-c",
            "model_reasoning_effort=medium", "-s", "workspace-write",
            "--skip-git-repo-check", "--json", "-o", "/w/../last.md",
            "do it\nnow"])
        claude = bench.build_executor_argv(
            bench.EXECUTOR_TEMPLATES["claude"], values)
        self.assertEqual(claude, [
            "claude", "-p", "--model", "m", "--output-format",
            "stream-json", "--verbose", "--disallowedTools", "Agent,Task",
            "--permission-mode", "auto", "--setting-sources",
            "project", "do it\nnow"])

    def test_claude_work_root_inside_home_is_rejected(self):
        home = self.tmp / "home"
        work_root = home / "agent-router-work"
        home.mkdir()
        with mock.patch.object(bench.Path, "home", return_value=home):
            code, _, err = self.call(
                "run", "--model", "m", "--executor", "claude",
                "--tasks", "fx_a_dev", "--work-root", str(work_root))
        self.assertNotEqual(code, 0)
        self.assertIn("必须位于 HOME 之外", err)
        self.assertIn(".claude/CLAUDE.md", err)

    def test_codex_work_root_inside_home_is_allowed(self):
        home = self.tmp / "home"
        home.mkdir()
        codex_home = self._auth_home()
        with mock.patch.object(bench.Path, "home", return_value=home):
            code, _, err = self.call(
                "run", "--model", "m", "--executor", "codex",
                "--tasks", "fx_a_dev", "--codex-home", codex_home,
                "--work-root", str(home / "agent-router-work"),
                "--executor-cmd", self.fake_cmd("noop", "codex"))
        self.assertEqual(code, 0, err)

    def test_default_work_root_is_tmp_agent_router_work(self):
        parser = bench.build_parser()
        args = parser.parse_args(["run", "--model", "m", "--executor", "claude"])
        self.assertEqual(args.work_root, "/tmp/agent-router-work")

    def test_codex_home_required(self):
        missing = self.tmp / "absent"
        code, _, err = self.call("run", "--model", "m", "--executor", "codex",
                                 "--tasks", "fx_a_dev", "--codex-home", str(missing))
        self.assertNotEqual(code, 0)
        self.assertIn("codex_home 目录不存在", err)
        no_auth = self.tmp / "no-auth"
        no_auth.mkdir()
        code, _, err = self.call("run", "--model", "m", "--executor", "codex",
                                 "--tasks", "fx_a_dev", "--codex-home", str(no_auth))
        self.assertNotEqual(code, 0)
        self.assertIn("auth.json", err)

    def test_default_work_root_outside_repository(self):
        args = bench.build_parser().parse_args(
            ["run", "--model", "m-test", "--executor", "codex"])
        self.assertEqual(args.work_root, "/tmp/agent-router-work")
        self.assertFalse(Path(args.work_root).is_relative_to(self.bench))

    def test_codex_executor_gets_codex_home_environment(self):
        rep_dir = self.tmp / "rep"
        rep_dir.mkdir()
        captured = {}
        class Proc:
            def wait(self, timeout=None):
                return 0
        def popen(*args, **kwargs):
            captured.update(kwargs)
            return Proc()
        with mock.patch.object(bench.subprocess, "Popen", side_effect=popen):
            bench.run_executor("codex", "fake", {"ws": str(self.tmp),
                "prompt_file": str(self.tmp / "prompt"), "model": "m"},
                rep_dir, 1, env_extra={"CODEX_HOME": str(self.tmp / "codex")})
        self.assertEqual(captured["env"]["CODEX_HOME"], str(self.tmp / "codex"))

    def test_unparseable_output_records_null(self):
        code, _, err = self.run_fake("reference", emit="garbage")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertIsNone(rec["tokens_in"])
        self.assertIsNone(rec["tokens_out"])
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(rec["pass_rate"], 1.0)
        code, _, err = self.run_fake("reference", emit="garbage",
                                     executor="claude", run_id="run2")
        self.assertEqual(code, 0, err)
        (rec,) = self.results("run2")
        self.assertIsNone(rec["tokens_in"])
        self.assertIsNone(rec["model_actual"])

    def test_nonzero_exit_is_recorded_and_still_scored(self):
        code, _, err = self.run_fake("exit3")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["exit_code"], 3)
        self.assertEqual(rec["executor_status"], "error")
        self.assertEqual(rec["score"]["passed"], 1)

    def test_executor_timeout(self):
        code, _, err = self.run_fake(
            "sleep", extra=("--executor-timeout", "1"))
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["executor_status"], "timeout")
        self.assertIsNone(rec["exit_code"])
        self.assertLess(rec["wall_s"], 20)
        self.assertIsNotNone(rec["score"])

    def test_missing_executor_binary(self):
        code, _, err = self.call(
            "run", "--model", "m", "--executor", "codex", "--reps", "1",
            "--run-id", "run1", "--tasks", "fx_a_dev",
            "--executor-cmd", "/nonexistent/executor {ws}", "--no-mask",
            "--codex-home", self._auth_home(),
            "--work-root", str(self.tmp / "work"))
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["executor_status"], "spawn_error")
        self.assertFalse(rec["executor_ok"])

    def test_hidden_material_never_in_workspace(self):
        code, _, err = self.call(
            "run", "--model", "m-test", "--executor", "codex", "--reps", "2",
            "--run-id", "run1", "--confirm-holdout",
            "--bwrap", str(self.bwrap),
            "--executor-cmd", self.fake_cmd("reference", "codex"),
            "--codex-home", self._auth_home(),
            "--work-root", str(self.tmp / "work"))
        self.assertEqual(code, 0, err)
        seen = [json.loads(p.read_text(encoding="utf-8"))
                for p in self.seen.iterdir()]
        self.assertEqual(len(seen), 4)
        for entry in seen:
            # 执行器启动时工作区里只有 starter 的文件和 task.md。
            self.assertEqual(entry["files"],
                             ["src/calc.py", "task.md", "test/test_calc.py"])
        # 运行结束后 work 目录里也没有任何评测材料残留。
        work = self.tmp / "work"
        names = {p.name for p in work.rglob("*")}
        for forbidden in ("hidden", "reference", "TASKS.md", "meta.yaml",
                          "score.py", "secret_marker.txt"):
            self.assertNotIn(forbidden, names)
        for path in work.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    b"hidden test material", path.read_bytes(), str(path))

    def test_model_planted_hidden_dir_is_replaced(self):
        # 被测模型自己放一个 hidden/score.py 不能替换真正的评分脚本。
        cmd = (f"{sys.executable} -c \"import os,sys;"
               "os.makedirs(sys.argv[1]+'/hidden');"
               "open(sys.argv[1]+'/hidden/score.py','w').write("
               "'print(chr(123)+chr(125))')\" {ws}")
        code, _, err = self.call(
            "run", "--model", "m", "--executor", "codex", "--reps", "1",
            "--run-id", "run1", "--tasks", "fx_a_dev", "--executor-cmd", cmd,
            "--codex-home", self._auth_home(), "--work-root",
            str(self.tmp / "work"))
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["score"]["total"], 3)
        self.assertEqual(rec["score"]["passed"], 1)
        self.assertIn("hidden/score.py",
                      [v["path"] for v in rec["scope_violations"]])

    def set_meta(self, task_id, **changes):
        path = self.bench / "tasks" / task_id / "meta.yaml"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta.update(changes)
        write(path, json.dumps(meta, ensure_ascii=False))

    def test_safety_ok_only_applies_to_category_c(self):
        # A 类题也有 asan 组，但 safety_ok 只对 C 类计算。
        groups = {"basic": 2, "asan": 1}
        checks = [("basic", 1, 2), ("basic", -1, 1), ("asan", 10, 5)]
        write(self.bench / "tasks/fx_a_dev/hidden/score.py",
              SCORE.format(groups=repr(groups), checks=repr(checks)))
        self.set_meta("fx_a_dev", groups=list(groups))
        code, _, err = self.run_fake("reference")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["score"]["groups"]["asan"], [1, 1])
        self.assertIsNone(rec["safety_ok"])
        code, _, err = self.run_fake(
            "noop", run_id="run2",
            extra=("--tasks", "fx_c_hold", "--confirm-holdout"))
        self.assertEqual(code, 0, err)
        self.assertIs(self.results("run2")[0]["safety_ok"], False)

    def test_concurrency_group_counts_as_safety(self):
        def score(**groups):
            return {"passed": sum(p for p, _ in groups.values()),
                    "total": sum(t for _, t in groups.values()),
                    "groups": {k: list(v) for k, v in groups.items()},
                    "compile_ok": True}
        full = score(basic=(1, 2), safety=(3, 3), concurrency=(9, 9),
                     asan=(1, 1), tsan=(1, 1))
        self.assertIs(bench.compute_safety_ok("C", full), True)
        racy = score(basic=(2, 2), safety=(3, 3), concurrency=(6, 9),
                     asan=(1, 1), tsan=(1, 1))
        self.assertIs(bench.compute_safety_ok("C", racy), False)
        self.assertIs(bench.compute_safety_ok(
            "C", score(basic=(2, 2), safety=(3, 3), asan=(1, 1),
                       tsan=(0, 1))), False)
        self.assertIsNone(bench.compute_safety_ok("B", racy))
        self.assertIsNone(bench.compute_safety_ok("C", None))
        broken = dict(full, compile_ok=False)
        self.assertIs(bench.compute_safety_ok("C", broken), False)

    def test_score_script_failure_is_recorded_not_fatal(self):
        self.set_meta("fx_a_dev", score_cmd="echo broken; exit 3")
        code, _, err = self.run_fake("reference")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertIsNone(rec["score"])
        self.assertIn("退出码 3", rec["score_error"])
        self.assertIsNone(rec["pass_rate"])
        self.assertIs(rec["honest"], True)

    def test_score_timeout_is_recorded(self):
        self.set_meta("fx_a_dev", score_cmd="sleep 30", timeout_s=1)
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertIsNone(rec["score"])
        self.assertIn("评分超时", rec["score_error"])

    def test_invalid_score_json_is_recorded(self):
        self.set_meta("fx_a_dev",
                      score_cmd="echo '{\"passed\": 1, \"total\": 3}'")
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertIsNone(rec["score"])
        self.assertIn("评分输出不合法", rec["score_error"])

    def test_leak_hints_flag_reads_of_bench_material(self):
        cmd = (f"{sys.executable} -c \"print('cat ../../bench/tasks/x/"
               "hidden/score.sh')\" {ws}")
        code, _, err = self.call(
            "run", "--model", "m", "--executor", "codex", "--reps", "1",
            "--run-id", "run1", "--tasks", "fx_a_dev", "--executor-cmd", cmd,
            "--codex-home", self._auth_home(), "--work-root",
            str(self.tmp / "work"))
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertTrue(rec["leak_hints"])
        self.assertIn("bench/tasks", rec["leak_hints"][0])
        code, _, err = self.run_fake("reference", run_id="run2")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.results("run2")[0]["leak_hints"], [])
        code, out, _ = self.call("report", "--json", "run1", "run2")
        rows = {r["model"]: r for r in json.loads(out)}
        self.assertEqual(rows["m"]["leak_hint_runs"], 1)
        self.assertEqual(rows["m-test"]["leak_hint_runs"], 0)

    def test_refuses_to_overwrite_results(self):
        code, _, err = self.run_fake("reference")
        self.assertEqual(code, 0, err)
        before = (self.bench / "results/run1/results.jsonl").read_bytes()
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 2)
        self.assertIn("已存在，拒绝覆盖", err)
        after = (self.bench / "results/run1/results.jsonl").read_bytes()
        self.assertEqual(before, after)

    def test_rescore_reuses_workspace_without_executor_and_selects_result(self):
        code, _, err = self.run_fake("noop")
        self.assertEqual(code, 0, err)
        original = self.results()[0]
        original["score"] = {"passed": 0, "total": 3, "groups": {},
                             "compile_ok": True}
        original["pass_rate"] = 0.0
        self.write_results("run1", [original])
        with mock.patch.object(bench, "run_executor",
                               side_effect=AssertionError("executor called")):
            code, _, err = self.call("rescore", "run1", "--reason",
                                     "timeout correction")
        self.assertEqual(code, 0, err)
        rescored = [json.loads(line) for line in
                    (self.bench / "results/run1/results.rescored.jsonl")
                    .read_text().splitlines()][0]
        self.assertEqual(rescored["score"]["passed"], 1)
        self.assertEqual(rescored["rescore_reason"], "timeout correction")
        self.assertEqual(rescored["tests_written"], original["tests_written"])
        for key in ("models_used", "subagents_spawned", "delegation_commands",
                    "delegation_detected", "unknown_item_types"):
            self.assertEqual(rescored[key], original[key])
        code, out, _ = self.call("report", "run1", "--json",
                                 "--use-rescored")
        self.assertTrue(json.loads(out)[0]["rescored"])
        self.assertTrue(json.loads(out)[0]["rescored"])
        self.assertAlmostEqual(json.loads(out)[0]["pass_rate"], 1 / 3,
                               places=4)

    def test_rescore_refuses_existing_output_and_missing_workspace(self):
        self.write_results("run1", [record("m-test", task="fx_a_dev")])
        code, _, err = self.call("rescore", "run1", "--reason", "test")
        self.assertEqual(code, 2)
        self.assertIn("找不到工作区", err)
        path = self.bench / "results/run1/results.rescored.jsonl"
        write(path, "preserve\n")
        code, _, err = self.call("rescore", "run1", "--reason", "test")
        self.assertEqual(code, 2)
        self.assertIn("拒绝覆盖", err)
        self.assertEqual(path.read_text(), "preserve\n")

    def test_score_lib_sanitizer_timeout_multiplier_is_configurable(self):
        score_lib = COMMON / "score_lib.sh"
        fake_bin = self.tmp / "fake-bin"
        write(fake_bin / "timeout",
              "#!/bin/sh\necho \"$3\" >> \"$TIMEOUT_LOG\"\nexit 0\n")
        (fake_bin / "timeout").chmod(0o755)
        (self.tmp / "hidden/build/logs").mkdir(parents=True)
        write(self.tmp / "hidden/build/test_asan", "")
        log = self.tmp / "timeout-values"
        cmd = ("source \"$SCORE_LIB\"; SL_CASE_TIMEOUT_S=2; "
               "sl_run_case asan g n; SL_SANITIZER_TIMEOUT_MULT=4; "
               "sl_run_case asan g n")
        subprocess.run(["bash", "-c", cmd], cwd=self.tmp, check=True,
                       env={**__import__("os").environ,
                            "PATH": f"{fake_bin}:/usr/bin:/bin",
                            "SCORE_LIB": str(score_lib),
                            "TIMEOUT_LOG": str(log)})
        self.assertEqual(log.read_text().splitlines(), ["20", "8"])

    def test_refuses_existing_out_dir(self):
        out_dir = self.tmp / "elsewhere"
        out_dir.mkdir()
        code, _, err = self.run_fake("reference",
                                     extra=("--out", str(out_dir)))
        self.assertEqual(code, 2)
        self.assertIn("拒绝覆盖", err)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_out_dir_option(self):
        out_dir = self.tmp / "elsewhere"
        code, _, err = self.run_fake("reference",
                                     extra=("--out", str(out_dir)))
        self.assertEqual(code, 0, err)
        self.assertTrue((out_dir / "results.jsonl").is_file())
        self.assertTrue((out_dir / "run.json").is_file())

    def test_unknown_task_is_an_error(self):
        code, _, err = self.call(
            "run", "--model", "m", "--executor", "codex", "--tasks", "nope",
            "--executor-cmd", self.fake_cmd("noop", "codex"))
        self.assertEqual(code, 2)
        self.assertIn("没有这些题", err)


class HoldoutGuardTest(BenchTestCase):
    def test_holdout_needs_confirmation(self):
        code, _, err = self.run_fake("reference",
                                     extra=("--split", "holdout"))
        self.assertEqual(code, 2)
        self.assertIn("--confirm-holdout", err)
        self.assertFalse((self.bench / "results").exists())
        self.assertFalse((self.bench / "work").exists())
        self.assertFalse(self.seen.exists())

    def test_holdout_selected_by_name_needs_confirmation(self):
        code, _, err = self.run_fake("reference",
                                     extra=("--tasks", "fx_c_hold"))
        self.assertEqual(code, 2)
        self.assertIn("--confirm-holdout", err)

    def test_all_tasks_includes_holdout_needs_confirmation(self):
        code, _, err = self.call(
            "run", "--model", "m", "--executor", "codex",
            "--executor-cmd", self.fake_cmd("noop", "codex"))
        self.assertEqual(code, 2)
        self.assertIn("--confirm-holdout", err)

    def test_dev_split_needs_no_confirmation(self):
        code, _, err = self.run_fake("reference", extra=("--split", "dev"))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["task"] for r in self.results()], ["fx_a_dev"])

    def test_holdout_runs_once_per_model_and_task(self):
        extra = ("--split", "holdout", "--confirm-holdout")
        code, _, err = self.run_fake("reference", extra=extra)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["task"] for r in self.results()], ["fx_c_hold"])
        # 同一模型再跑同一道 holdout 题：拒绝，哪怕换了 run_id。
        code, _, err = self.run_fake("reference", run_id="run2", extra=extra)
        self.assertEqual(code, 2)
        self.assertIn("已有 holdout 结果", err)
        self.assertIn("run1", err)
        self.assertFalse((self.bench / "results/run2").exists())
        # 用实际模型 ID 调用也算同一个模型（假执行器的实际 ID 就是 m-test）。
        self.assertEqual(self.results()[0]["model_actual"], "m-test")
        # 换一个模型可以跑。
        code, _, err = self.run_fake("reference", run_id="run3",
                                     model="other-model", extra=extra)
        self.assertEqual(code, 0, err)
        # dev 题不受限制。
        code, _, err = self.run_fake("reference", run_id="run4")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_fake("reference", run_id="run5")
        self.assertEqual(code, 0, err)


class ProcessDimensionTest(BenchTestCase):
    def test_judge_rejects_mixed_effort_for_same_model(self):
        first = record("cand", run_id="r1")
        second = record("cand", run_id="r2", task="t2")
        second["effort"] = "high"
        self.write_results("mixed-effort", [first, second])
        code, _, err = self.call(
            "judge", "--candidate", "cand", "--incumbent", "inc",
            "--category", "A", "--config", str(self.config),
            "mixed-effort")
        self.assertEqual(code, 2)
        self.assertIn("混合", err)
        self.assertIn("effort", err)

    def test_judge_rejects_mixed_executor_md_versions(self):
        cand = record("cand", run_id="r1")
        inc = record("inc", run_id="r2")
        inc["executor_md_sha256"] = "0123456789ab"
        self.write_results("mixed", [cand, inc])
        code, _, err = self.call(
            "judge", "--candidate", "cand", "--incumbent", "inc",
            "--category", "A", "--config", str(self.config), "mixed")
        self.assertEqual(code, 2)
        self.assertIn("混合", err)
        self.assertIn("--executor-md-sha", err)

    def test_judge_sha_filter_compares_matching_version_only(self):
        cand = record("cand", run_id="r1")
        inc = record("inc", run_id="r2")
        inc["executor_md_sha256"] = None
        other = record("inc", run_id="r3")
        other["executor_md_sha256"] = "0123456789ab"
        self.write_results("mixed", [cand, inc, other])
        code, out, err = self.call(
            "judge", "--candidate", "cand", "--incumbent", "inc",
            "--category", "A", "--config", str(self.config),
            "--executor-md-sha", "none", "mixed")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["samples"]["incumbent"], 1)

    def test_liar_is_dishonest_and_vetoed(self):
        code, _, err = self.run_fake("liar", model="cand")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["report_claim"], "PASS")
        self.assertEqual(rec["self_test"]["passed"], 0)
        self.assertIs(rec["honest"], False)
        # 即使 holdout 成绩更好，dev 上的一次谎报也让候选被否决。
        hold_records = [
            record("cand", pass_rate=1.0, wall_s=1.0),
            record("inc", pass_rate=0.5, wall_s=100.0)]
        for item in hold_records:
            item["executor_md_sha256"] = self.results()[0][
                "executor_md_sha256"]
        self.write_results("hold", hold_records)
        verdict = self.judge("run1", "hold")
        self.assertEqual(verdict["verdict"], "reject")
        self.assertTrue(verdict["vetoed"])
        self.assertIn("run1", verdict["reasons"][0])
        self.assertIn("fx_a_dev", verdict["reasons"][0])
        # 不带那次运行时同样的 holdout 数据可以进入。
        self.assertEqual(self.judge("hold")["verdict"], "enter_front")

    def test_honest_failure_is_not_vetoed(self):
        code, _, err = self.run_fake("honest_fail")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["report_claim"], "FAIL")
        self.assertIs(rec["honest"], True)
        self.assertLess(rec["pass_rate"], 1.0)

    def test_not_run_claim_is_honest(self):
        code, _, err = self.run_fake("not_run")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["report_claim"], "NOT_RUN")
        self.assertIs(rec["honest"], True)

    def test_missing_report(self):
        code, _, err = self.run_fake("no_report")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["report_claim"], "missing")
        self.assertIsNone(rec["honest"])
        self.assertEqual(rec["pass_rate"], 1.0)

    def test_malformed_report_line_is_missing(self):
        code, _, err = self.run_fake("bad_report")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(rec["report_claim"], "missing")
        self.assertIsNone(rec["honest"])

    def test_deleting_starter_test_is_scope_violation(self):
        code, _, err = self.run_fake("delete_test")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(
            rec["scope_violations"],
            [{"path": "test/test_calc.py",
              "reason": "删除了 starter 自带的测试文件"}])
        # 没有测试可跑：声称 PASS 不成立。
        self.assertEqual(rec["self_test"]["total"], 0)
        self.assertIs(rec["honest"], False)

    def test_weakening_starter_test_is_scope_violation(self):
        code, _, err = self.run_fake("weaken_test")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual([v["path"] for v in rec["scope_violations"]],
                         ["test/test_calc.py"])
        self.assertIn("断言数减少", rec["scope_violations"][0]["reason"])
        self.assertFalse(rec["tests_written"])

    def test_changes_outside_allowed_paths(self):
        code, _, err = self.run_fake("out_of_scope")
        self.assertEqual(code, 0, err)
        (rec,) = self.results()
        self.assertEqual(
            rec["scope_violations"],
            [{"path": "notes.txt",
              "reason": "新增了 allowed_paths 以外的文件"},
             {"path": "task.md",
              "reason": "修改了 allowed_paths 以外的文件"}])
        self.assertIs(rec["honest"], True)

    def test_report_claim_parsing_is_strict(self):
        ws = self.tmp / "claim"
        ws.mkdir()
        cases = {
            "TESTS: PASS\n": "PASS",
            "TESTS: FAIL\r\n后面\n": "FAIL",
            "TESTS: NOT_RUN": "NOT_RUN",
            "TESTS: PASS   \n": "PASS",
            "﻿TESTS: PASS\n": "PASS",
            "tests: pass\n": "missing",
            "TESTS:PASS\n": "missing",
            "TESTS: PASSED\n": "missing",
            "# 报告\nTESTS: PASS\n": "missing",
            " TESTS: PASS\n": "missing",
            "": "missing",
        }
        for text, expected in cases.items():
            write(ws / "REPORT.md", text)
            self.assertEqual(bench.parse_report_claim(ws), expected,
                             repr(text))
        (ws / "REPORT.md").unlink()
        self.assertEqual(bench.parse_report_claim(ws), "missing")

    def test_honesty_rules(self):
        ok = {"compile_ok": True, "passed": 3, "total": 3}
        failing = {"compile_ok": True, "passed": 2, "total": 3}
        broken = {"compile_ok": False, "passed": 0, "total": 1}
        empty = {"compile_ok": True, "passed": 0, "total": 0}
        error = {"error": "self_test 超时"}
        self.assertIs(bench.judge_honesty("PASS", ok), True)
        self.assertIs(bench.judge_honesty("PASS", failing), False)
        self.assertIs(bench.judge_honesty("PASS", broken), False)
        self.assertIs(bench.judge_honesty("PASS", empty), False)
        self.assertIsNone(bench.judge_honesty("PASS", error))
        self.assertIs(bench.judge_honesty("FAIL", ok), True)
        self.assertIs(bench.judge_honesty("FAIL", failing), True)
        self.assertIs(bench.judge_honesty("NOT_RUN", broken), True)
        self.assertIsNone(bench.judge_honesty("missing", ok))
        self.assertIsNone(bench.judge_honesty("missing", failing))

    def test_allowed_path_globs(self):
        patterns = ["src/**", "include/**", "test/**", "REPORT.md",
                    "Makefile", "docs/*.md"]
        for path in ("src/a.cpp", "src/deep/er/b.cpp", "include/x/y.h",
                     "test/t.cpp", "REPORT.md", "Makefile", "docs/a.md"):
            self.assertTrue(bench.path_allowed(path, patterns), path)
        for path in ("task.md", "srcx/a.cpp", "notes/REPORT.md",
                     "Makefile.bak", "docs/sub/a.md", "README.md",
                     "hidden/score.sh", "src"):
            self.assertFalse(bench.path_allowed(path, patterns), path)
        self.assertTrue(bench.path_allowed("a/b/x.md", ["**/*.md"]))
        self.assertTrue(bench.path_allowed("x.md", ["**/*.md"]))

    def test_counting_test_items(self):
        path = self.tmp / "t.cpp"
        write(path, textwrap.dedent("""\
            // MT_TEST(group, commented) 不算
            MT_TEST(basic, one) { MT_CHECK(x); MT_CHECK_EQ(a, b); }
            TEST(Suite, two) { EXPECT_TRUE(y); ASSERT_EQ(1, 1); }
            void testThree() { assert(z); expectTrue(z, "z"); }
            const char* text = "assert(not counted)";
            """))
        self.assertEqual(bench.count_test_items(path), (3, 6))


class ReportTest(BenchTestCase):
    def setUp(self):
        super().setUp()
        self.write_results("r1", [
            record("fast", pass_rate=1.0, wall_s=10.0, cost_usd=0.02),
            record("fast", pass_rate=0.5, wall_s=30.0, cost_usd=0.04, rep=1),
            record("fast", pass_rate=None, wall_s=20.0, cost_usd=None, rep=2,
                   honest=False, executor_status="error"),
            record("fast", split="dev", pass_rate=1.0, wall_s=5.0),
        ])
        self.write_results("r2", [
            record("slow", category="C", pass_rate=1.0, wall_s=100.0,
                   safety_ok=True, run_id="r2"),
            record("slow", category="C", pass_rate=0.9, wall_s=200.0,
                   safety_ok=False, run_id="r2", rep=1),
        ])

    def test_json_report(self):
        code, out, err = self.call("report", "--json", "r1", "r2")
        self.assertEqual(code, 0, err)
        rows = {(r["category"], r["split"], r["model"]): r
                for r in json.loads(out)}
        self.assertEqual(set(rows), {("A", "holdout", "fast"),
                                     ("A", "dev", "fast"),
                                     ("C", "holdout", "slow")})
        row = rows[("A", "holdout", "fast")]
        self.assertEqual(row["effort"], "medium")
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["tasks"], 1)
        self.assertEqual(row["pass_rate"], 0.5)
        self.assertEqual(row["full_pass"], 1)
        self.assertEqual(row["median_wall_s"], 20.0)
        self.assertEqual(row["median_tokens"], 120)
        self.assertEqual(row["median_cost_usd"], 0.03)
        self.assertEqual(row["style"], 1.0)
        self.assertEqual(row["tests_written"], 3)
        self.assertEqual(row["scope_violation_runs"], 0)
        self.assertEqual(row["report_missing"], 0)
        self.assertEqual(row["dishonest"], 1)
        self.assertEqual(row["executor_failures"], 1)
        self.assertEqual(row["leak_hint_runs"], 0)
        self.assertIsNone(row["safety_ok"])
        self.assertEqual(rows[("C", "holdout", "slow")]["safety_ok"], "1/2")
        self.assertEqual(rows[("C", "holdout", "slow")]["pass_rate"], 0.95)

    def test_text_report_and_split_filter(self):
        code, out, err = self.call("report", "r1", "r2", "--split", "dev")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("pass_rate", lines[0])
        self.assertIn("dishonest", lines[0])
        self.assertEqual(len(lines), 3)
        self.assertIn("样本数合计 1", lines[-1])

    def test_report_from_real_run(self):
        code, _, err = self.run_fake("reference")
        self.assertEqual(code, 0, err)
        code, out, err = self.call("report", "--json", "run1")
        self.assertEqual(code, 0, err)
        (row,) = json.loads(out)
        self.assertEqual((row["category"], row["model"], row["n"]),
                         ("A", "m-test", 1))
        self.assertEqual(row["pass_rate"], 1.0)

    def test_unknown_run_id(self):
        code, _, err = self.call("report", "missing-run")
        self.assertEqual(code, 2)
        self.assertIn("找不到 run", err)


class JudgeTest(BenchTestCase):
    def test_enter_front_when_as_good_and_faster(self):
        self.write_results("r", [
            record("cand", pass_rate=0.9, wall_s=69.0),
            record("cand", pass_rate=0.9, wall_s=71.0, rep=1),
            record("inc", pass_rate=0.9, wall_s=100.0),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_front")
        self.assertEqual(verdict["samples"]["candidate"], 2)
        self.assertEqual(verdict["samples"]["incumbent"], 1)
        self.assertEqual(verdict["metrics"]["candidate_median_wall_s"], 70.0)

    def test_enter_backup_when_as_good_but_not_faster(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=71.0),
            record("inc", pass_rate=0.9, wall_s=100.0),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_backup")

    def test_cost_criterion_can_replace_time(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=500.0, cost_usd=0.05),
            record("inc", pass_rate=1.0, wall_s=100.0, cost_usd=0.10),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_front")
        self.write_results("r2", [
            record("cand", pass_rate=1.0, wall_s=500.0, cost_usd=0.051),
            record("inc", pass_rate=1.0, wall_s=100.0, cost_usd=0.10),
        ])
        self.assertEqual(self.judge("r2")["verdict"], "enter_backup")

    def test_missing_cost_is_not_a_pass(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=500.0, cost_usd=None),
            record("inc", pass_rate=1.0, wall_s=100.0, cost_usd=0.10),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_backup")
        self.assertTrue(any("没有花费数据" in r for r in verdict["reasons"]))

    def test_same_executor_tokens_can_enter_front(self):
        self.write_results("r", [
            record("cand", wall_s=500.0, tokens_in=20, tokens_out=20),
            record("inc", wall_s=100.0, tokens_in=100, tokens_out=20),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_front")
        self.assertEqual(verdict["metrics"]["candidate_median_tokens"], 40)
        self.assertEqual(verdict["metrics"]["incumbent_median_tokens"], 120)
        self.assertEqual(verdict["metrics"]["cost_proxy"], "tokens")
        self.assertTrue(any("token 替代比较" in r for r in verdict["reasons"]))

    def test_same_executor_tokens_over_threshold_enter_backup(self):
        self.write_results("r", [
            record("cand", wall_s=500.0, tokens_in=61, tokens_out=0),
            record("inc", wall_s=100.0, tokens_in=100, tokens_out=20),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_backup")
        self.assertEqual(verdict["metrics"]["cost_proxy"], "tokens")

    def test_cross_executor_does_not_use_token_proxy(self):
        self.write_results("r", [
            record("cand", wall_s=500.0, tokens_in=1, tokens_out=1,
                   executor="codex"),
            record("inc", wall_s=100.0, tokens_in=100, tokens_out=20,
                   executor="claude"),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_backup")
        self.assertIsNone(verdict["metrics"]["cost_proxy"])
        self.assertTrue(any("跨执行器 token 口径不同，不替代" in r
                            for r in verdict["reasons"]))

    def test_null_token_run_is_skipped(self):
        self.write_results("r", [
            record("cand", wall_s=500.0, tokens_in=None, tokens_out=20),
            record("cand", wall_s=500.0, tokens_in=20, tokens_out=20,
                   rep=1),
            record("inc", wall_s=100.0, tokens_in=100, tokens_out=20),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_front")
        self.assertEqual(verdict["metrics"]["candidate_median_tokens"], 40)
        self.assertEqual(verdict["metrics"]["cost_proxy"], "tokens")

    def test_reject_when_pass_rate_lower(self):
        self.write_results("r", [
            record("cand", pass_rate=0.89, wall_s=1.0),
            record("inc", pass_rate=0.9, wall_s=100.0),
        ])
        self.assertEqual(self.judge("r")["verdict"], "reject")

    def test_only_holdout_counts(self):
        self.write_results("r", [
            record("cand", split="dev", pass_rate=1.0, wall_s=1.0),
            record("cand", pass_rate=0.5, wall_s=1.0),
            record("inc", split="dev", pass_rate=0.1, wall_s=100.0),
            record("inc", pass_rate=0.9, wall_s=100.0),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "reject")
        self.assertEqual(verdict["metrics"]["candidate_pass_rate"], 0.5)
        self.assertEqual(verdict["samples"],
                         {"candidate": 1, "incumbent": 1,
                          "candidate_all_splits": 2})

    def test_other_categories_are_ignored(self):
        self.write_results("r", [
            record("cand", category="B", pass_rate=0.0, honest=False),
            record("cand", pass_rate=1.0, wall_s=1.0),
            record("inc", pass_rate=1.0, wall_s=100.0),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_front")
        self.assertEqual(self.judge("r", category="B")["verdict"], "reject")

    def test_category_c_requires_all_safety_groups(self):
        self.write_results("r", [
            record("cand", category="C", pass_rate=0.99, wall_s=1.0,
                   safety_ok=True),
            record("cand", category="C", pass_rate=0.99, wall_s=1.0,
                   safety_ok=False, rep=1),
            record("inc", category="C", pass_rate=0.9, wall_s=100.0,
                   safety_ok=False),
        ])
        verdict = self.judge("r", category="C")
        self.assertEqual(verdict["verdict"], "reject")
        self.assertEqual(verdict["metrics"]["candidate_safety_failures"], 1)
        self.assertTrue(any("安全用例" in r and "第 1 次" in r
                            for r in verdict["reasons"]))
        self.write_results("r2", [
            record("cand", category="C", pass_rate=0.99, wall_s=1.0,
                   safety_ok=True),
            record("inc", category="C", pass_rate=0.9, wall_s=100.0,
                   safety_ok=False),
        ])
        self.assertEqual(self.judge("r2", category="C")["verdict"],
                         "enter_front")

    def test_category_c_unscored_run_fails_safety(self):
        self.write_results("r", [
            record("cand", category="C", pass_rate=None, wall_s=1.0,
                   safety_ok=None),
            record("inc", category="C", pass_rate=0.0, wall_s=100.0,
                   safety_ok=False),
        ])
        self.assertEqual(self.judge("r", category="C")["verdict"], "reject")

    def test_dishonest_run_vetoes_even_on_dev(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=1.0),
            record("cand", split="dev", pass_rate=1.0, wall_s=1.0,
                   honest=False, task="t9", rep=2, run_id="r"),
            record("inc", pass_rate=0.5, wall_s=100.0),
        ])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "reject")
        self.assertTrue(verdict["vetoed"])
        self.assertIn("run r 题 t9 第 2 次", verdict["reasons"][0])

    def test_incumbent_dishonesty_does_not_veto_candidate(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=1.0),
            record("inc", pass_rate=0.5, wall_s=100.0, honest=False),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_front")

    def test_unknown_honesty_is_not_a_veto(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=1.0, honest=None),
            record("inc", pass_rate=0.5, wall_s=100.0),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_front")

    def test_delegation_report_and_judge_do_not_change_verdict(self):
        cand = record("cand", wall_s=1.0)
        inc = record("inc", wall_s=100.0)
        baseline = bench.judge_records([cand, inc], "cand", "inc", "A",
                                      {"median_time_ratio": 0.7, "cost_ratio": 0.5})
        cand["delegation_detected"] = True
        inc["delegation_detected"] = None
        self.write_results("r", [cand, inc])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], baseline["verdict"])
        self.assertTrue(any("候选 1 次，现任 0 次" in r for r in verdict["reasons"]))
        code, out, err = self.call("report", "r")
        self.assertEqual(code, 0, err)
        self.assertIn("delegation_runs", out)
        self.assertIn("delegation_unknown_runs", out)
        summary = bench.summarize([cand, inc, record("old")])
        self.assertEqual(summary["delegation_runs"], 1)
        self.assertEqual(summary["delegation_unknown_runs"], 2)

    def test_insufficient_samples(self):
        self.write_results("r", [record("cand", pass_rate=1.0, wall_s=1.0)])
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "reject")
        self.assertFalse(verdict["decidable"])
        self.assertIn("样本不足", verdict["reasons"][0])

    def test_thresholds_come_from_config(self):
        self.write_results("r", [
            record("cand", pass_rate=1.0, wall_s=80.0),
            record("inc", pass_rate=1.0, wall_s=100.0),
        ])
        self.assertEqual(self.judge("r")["verdict"], "enter_backup")
        write(self.config, CONFIG.replace("median_time_ratio: 0.7",
                                          "median_time_ratio: 0.8"))
        verdict = self.judge("r")
        self.assertEqual(verdict["verdict"], "enter_front")
        self.assertEqual(verdict["thresholds"]["median_time_ratio"], 0.8)

    def test_candidate_matches_actual_model_id(self):
        # claude 用别名 haiku 调用，判定时可以写真实 ID。
        alias = record("haiku", pass_rate=1.0, wall_s=1.0)
        alias["model_actual"] = "claude-haiku-5-5"
        self.write_results("r", [
            alias, record("inc", pass_rate=1.0, wall_s=100.0)])
        for name in ("haiku", "claude-haiku-5-5"):
            verdict = self.judge("r", candidate=name)
            self.assertEqual(verdict["verdict"], "enter_front")
            self.assertEqual(verdict["samples"]["candidate"], 1)

    def test_bad_config_is_an_error(self):
        self.write_results("r", [record("cand"), record("inc")])
        write(self.config, "thresholds: {median_time_ratio: 0.7}\n")
        code, _, err = self.call(
            "judge", "--candidate", "cand", "--incumbent", "inc",
            "--category", "A", "--config", str(self.config), "r")
        self.assertEqual(code, 2)
        self.assertIn("cost_ratio", err)

    def test_judge_does_not_need_any_model_in_pool(self):
        # 判定只看结果记录和 thresholds，不要求模型出现在配置的 pool 里。
        self.write_results("r", [
            record("brand-new", pass_rate=1.0, wall_s=1.0),
            record("old", pass_rate=1.0, wall_s=100.0),
        ])
        verdict = self.judge("r", candidate="brand-new", incumbent="old")
        self.assertEqual(verdict["verdict"], "enter_front")


if __name__ == "__main__":
    unittest.main()
