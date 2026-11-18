import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import yaml
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "router.py"
if not MODULE.is_file():
    MODULE = ROOT / "tools" / "router.py"
SPEC = importlib.util.spec_from_file_location("router", MODULE)
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "models.yaml"
        self.config.write_text(yaml.safe_dump({
            "pool": [
                {"id": "gpt-6-luna", "executor": "codex", "status": "active", "evaluated": True, "note": "test model", "alias": "luna"},
                {"id": "gpt-6.1-sol", "executor": "codex", "status": "active", "evaluated": True, "note": "test model", "effort": "high"},
                {"id": "gpt-6-astra", "executor": "codex", "status": "active", "evaluated": True, "note": "test model"},
                {"id": "gpt-6.2-sol", "executor": "codex", "status": "candidate", "evaluated": False, "note": "candidate", "effort": "high"},
                {"id": "deepseek-flash", "executor": "dsh", "status": "retired", "evaluated": True, "note": "retired test model"},
                {"id": "claude-haiku-5-5", "executor": "claude", "status": "active", "evaluated": True, "note": "test model", "alias": "haiku"},
            ],
            "routing": {
                "A": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", "gpt-6.2-sol", "deepseek-flash", "claude-haiku-5-5"],
                "B": ["gpt-6-luna", "gpt-6.1-sol"],
                "C": ["gpt-6-luna"],
            },
            "rework": {"same_defect_rounds": 2, "total_rounds": 3},
            "thresholds": {
                "median_time_ratio": 1.5, "cost_ratio": 2.0,
                "reeval_min_records": 8, "reeval_window": 20, "reeval_rate": 0.3,
                "rework_records_min": 8, "rework_rescue_rate": 0.3,
            },
        }, sort_keys=False), encoding="utf-8")
        self.ledger = self.root / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = router.main(["--config", str(self.config), "--ledger", str(self.ledger), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def log(self, task, category="A", attempt=0, outcome="pass", **extra):
        argv = ["log", "--task-id", task, "--category", category, "--executor", "codex",
                "--model", "gpt-6-luna", "--attempt", str(attempt), "--outcome", outcome]
        for k, v in extra.items():
            argv += ["--" + k.replace("_", "-"), str(v)]
        return self.run_cli(*argv)

    def test_route_skips_non_active_and_rejects_unknown_category(self):
        code, out, _ = self.run_cli("route", "A")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "gpt-6-luna\tmedium", "gpt-6.1-sol\thigh",
            "gpt-6-astra\tmedium", "claude-haiku-5-5\t-",
        ])
        self.assertNotIn("deepseek-flash", out)
        self.assertNotIn("gpt-6.2-sol", out)
        code, _, err = self.run_cli("route", "D")
        self.assertNotEqual(code, 0)
        self.assertIn("A, B, or C", err)

    def test_effort_is_rejected_for_non_codex_and_when_invalid(self):
        cfg = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        next(model for model in cfg["pool"]
             if model["id"] == "claude-haiku-5-5")["effort"] = "low"
        self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with self.assertRaisesRegex(router.RouterError,
                                    "only valid for codex"):
            router.load_config(self.config)

        cfg = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        claude = next(model for model in cfg["pool"]
                      if model["id"] == "claude-haiku-5-5")
        del claude["effort"]
        next(model for model in cfg["pool"]
             if model["id"] == "gpt-6-luna")["effort"] = "extreme"
        self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with self.assertRaisesRegex(router.RouterError, "effort must be"):
            router.load_config(self.config)

    def test_candidate_is_allowed_in_ledger(self):
        code, _, err = self.run_cli(
            "log", "--task-id", "candidate-task", "--category", "A",
            "--executor", "codex", "--model", "gpt-6.2-sol",
            "--attempt", "0", "--outcome", "pass")
        self.assertEqual(code, 0, err)
        row = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(row["model"], "gpt-6.2-sol")

    def test_real_config_passes_format_validation(self):
        config = ROOT / "models.example.yaml"
        if not config.is_file():
            config = ROOT / "config" / "models.yaml"
        cfg = router.load_config(config)
        self.assertIsInstance(cfg, dict)

    def test_relocated_skill_defaults_use_local_private_config_and_ledger(self):
        skill = self.root / "model-routing"
        (skill / "config").mkdir(parents=True)
        (skill / "config/models.yaml").write_bytes(self.config.read_bytes())
        with mock.patch.object(router, "__file__", str(skill / "router.py")):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = router.main([
                    "log", "--task-id", "relocated", "--category", "A",
                    "--executor", "codex", "--model", "gpt-6-luna",
                    "--attempt", "0", "--outcome", "pass"])
        self.assertEqual(code, 0)
        rows = (skill / "ledger/ledger.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(rows[0])["task_id"], "relocated")

    def test_log_validates_and_appends_without_overwriting(self):
        self.assertEqual(self.log("one")[0], 0)
        original = self.ledger.read_text(encoding="utf-8")
        self.assertEqual(self.log("two", attempt=1, outcome="fail_test", note="retry")[0], 0)
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(self.ledger.read_text(encoding="utf-8").startswith(original))
        self.assertEqual(json.loads(lines[0])["attempt"], 0)
        code, _, err = self.log("bad", outcome="invalid")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid outcome", err)

    def test_partial_is_accepted_and_counted_separately(self):
        self.assertEqual(self.log("partial", outcome="partial")[0], 0)
        self.assertEqual(self.log("passed", outcome="pass")[0], 0)
        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        row = json.loads(out)
        self.assertEqual(row["first_pass_rate"], 1.0)
        self.assertEqual(row["partial_rate"], 0.5)

    def test_bad_ledger_lines_are_skipped_and_reported(self):
        self.assertEqual(self.log("valid")[0], 0)
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write("{bad json}\n")
            stream.write(json.dumps({"outcome": "bogus"}) + "\n")
        code, out, err = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["records"], 1)
        self.assertIn("skipped 2 invalid ledger line(s)", err)
        self.assertIn("line 2:", err)
        self.assertIn("line 3:", err)
        code, _, err = self.run_cli("check-reeval")
        self.assertEqual(code, 0)
        self.assertIn("skipped 2 invalid ledger line(s)", err)

    def test_stats_and_empty_ledger(self):
        code, out, _ = self.run_cli("stats")
        self.assertEqual((code, out), (0, ""))
        self.log("t1", outcome="pass", escalated_to="gpt-6.1-sol")
        self.log("t1", attempt=1, outcome="pass")
        code, out, _ = self.run_cli("stats", "--category", "A", "--last", "2")
        row = json.loads(out)
        self.assertEqual(row["records"], 2)
        self.assertEqual(row["first_pass_rate"], 1.0)
        self.assertEqual(row["escalation_rate"], 0.5)
        self.assertEqual(row["rework_rate"], 0.5)
        self.assertEqual(row["avg_rework_rounds"], 1.0)

    def test_detect_codex_and_claude(self):
        cache = self.root / "cache.json"
        cache.write_text(json.dumps({"models": [
            {"slug": "gpt-6-luna", "visibility": "list"},
            {"slug": "new-model", "visibility": "list"},
            {"slug": "hidden-model", "visibility": "hide"}]}), encoding="utf-8")
        code, out, _ = self.run_cli("detect", "--codex-cache", cache, "--claude-ids", "claude-haiku-5-5,claude-haiku-5-6")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"codex": ["new-model"], "claude": ["claude-haiku-5-6"]})
        code, _, err = self.run_cli("detect", "--codex-cache", self.root / "missing")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read Codex cache", err)

    def fill_eight(self, attempts):
        for i in range(8):
            self.log(f"t{i}", attempt=1 if i in attempts else 0,
                     outcome="fail_test" if i in attempts else "pass",
                     **({"escalated_to": "gpt-6.1-sol"} if i in attempts else {}))

    def test_check_reeval_boundaries(self):
        self.fill_eight(set())
        code, out, _ = self.run_cli("check-reeval")
        self.assertEqual(json.loads(out)["categories"], [])
        self.ledger.unlink()
        self.fill_eight({0, 1, 2})  # exactly 3/8 is >30%; use 6/20 for exact boundary below
        code, out, _ = self.run_cli("check-reeval")
        self.assertEqual(json.loads(out)["categories"], ["A"])
        self.ledger.unlink()
        # Two reworked records among eight are 25%, below threshold.
        self.fill_eight({0, 1})
        code, out, _ = self.run_cli("check-reeval")
        self.assertEqual(json.loads(out)["categories"], [])

    def test_check_reeval_exact_eight_and_exact_thirty_percent(self):
        # Exactly eight rows are sufficient. Three elevated rows among eight exceed 30%.
        self.fill_eight({0, 1, 2})
        code, out, _ = self.run_cli("check-reeval")
        data = json.loads(out)
        self.assertEqual(data["rates"]["A"]["records"], 8)
        self.assertEqual(data["categories"], ["A"])
        # 6/20 is exactly 30%; it must not trigger.
        self.ledger.unlink()
        for i in range(20):
            self.log(f"boundary-{i}", attempt=1 if i < 6 else 0,
                     outcome="fail_test" if i < 6 else "pass")
        code, out, _ = self.run_cli("check-reeval")
        data = json.loads(out)
        self.assertEqual(data["rates"]["A"]["rework_rate"], 0.3)
        self.assertEqual(data["categories"], [])

    def test_optional_alias_validation(self):
        cfg = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        claude = [m for m in cfg["pool"] if m["executor"] == "claude"]
        self.assertEqual([(m["id"], m["alias"]) for m in claude], [("claude-haiku-5-5", "haiku")])
        for alias in (None, "", "   ", 42, []):
            with self.subTest(alias=alias):
                cfg["pool"][0]["alias"] = alias
                self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
                code, _, err = self.run_cli("route", "A")
                self.assertNotEqual(code, 0)
                self.assertIn("alias must be a non-empty string", err)
        cfg["pool"][0]["alias"] = "luna"
        self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        self.assertEqual(self.run_cli("route", "A")[0], 0)

    def test_check_reeval_recent_window_sorted_by_timestamp(self):
        rows = []
        for category in ("A", "B"):
            for i in range(40):
                failed = (i < 20) if category == "A" else (i >= 20)
                code, out, _ = self.log(f"{category}-{i}", category=category,
                                        attempt=int(failed), outcome="fail_test" if failed else "pass",
                                        **({"escalated_to": "gpt-6.1-sol"} if failed else {}))
                self.assertEqual(code, 0)
                row = json.loads(out)
                row["ts"] = f"2026-11-13T00:00:{i:02d}+00:00"
                rows.append(row)
        # Reverse append order to ensure selection follows ts, per category.
        self.ledger.write_text("".join(json.dumps(row) + "\n" for row in reversed(rows)), encoding="utf-8")
        code, out, _ = self.run_cli("check-reeval")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["categories"], ["B"])
        self.assertEqual(data["rates"]["A"], {"records": 20, "upgrade_rate": 0.0, "rework_rate": 0.0})
        self.assertEqual(data["rates"]["B"], {"records": 20, "upgrade_rate": 1.0, "rework_rate": 1.0})

    def test_check_reeval_insufficient_window_records(self):
        for i in range(7):
            self.assertEqual(self.log(str(i), attempt=1)[0], 0)
        code, out, _ = self.run_cli("check-reeval")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"categories": [], "rates": {}})

    def test_reeval_window_and_timestamp_validation(self):
        cfg = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        for window in (0, -1, 1.5, True, "20"):
            with self.subTest(window=window):
                cfg["thresholds"]["reeval_window"] = window
                self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
                self.assertNotEqual(self.run_cli("check-reeval")[0], 0)
        cfg["thresholds"]["reeval_window"] = 20
        self.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        _, out, _ = self.log("bad-time")
        row = json.loads(out)
        row["ts"] = "not-a-timestamp"
        self.ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
        code, _, err = self.run_cli("check-reeval")
        self.assertEqual(code, 0)
        self.assertIn("line 1:", err)
        self.assertIn("ts must be an ISO8601 timestamp", err)


if __name__ == "__main__":
    unittest.main()
