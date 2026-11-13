#!/usr/bin/env python3
"""测试用的假执行器：按 --mode 改工作区，按 --emit 模仿真实执行器的输出格式。

不是测试用例文件（unittest 不会收集它），由 tests/test_bench.py 通过
bench.py run 的 --executor-cmd 调用。
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time


def copy_reference(bench, task, ws):
    shutil.copytree(Path(bench) / "tasks" / task / "reference", ws,
                    dirs_exist_ok=True)


def break_source(ws):
    path = Path(ws) / "src" / "calc.py"
    path.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")


def set_report(ws, first_line):
    path = Path(ws) / "REPORT.md"
    path.write_text(first_line + "\n\n说明。\n", encoding="utf-8")


def apply_mode(mode, bench, task, ws):
    if mode in ("noop", "dsh_incomplete_noop"):
        return 0
    if mode == "exit3":
        return 3
    if mode == "sleep":
        time.sleep(30)
        return 0
    copy_reference(bench, task, ws)
    if mode == "liar":
        break_source(ws)
        set_report(ws, "TESTS: PASS")
    elif mode == "honest_fail":
        break_source(ws)
        set_report(ws, "TESTS: FAIL")
    elif mode == "not_run":
        break_source(ws)
        set_report(ws, "TESTS: NOT_RUN")
    elif mode == "delete_test":
        (Path(ws) / "test" / "test_calc.py").unlink()
    elif mode == "weaken_test":
        (Path(ws) / "test" / "test_calc.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
    elif mode == "out_of_scope":
        (Path(ws) / "notes.txt").write_text("scratch\n", encoding="utf-8")
        with open(Path(ws) / "task.md", "a", encoding="utf-8") as f:
            f.write("\n改了任务说明\n")
    elif mode == "no_report":
        (Path(ws) / "REPORT.md").unlink()
    elif mode == "bad_report":
        set_report(ws, "tests: pass")
    elif mode != "reference":
        raise SystemExit(f"unknown mode {mode}")
    return 0


def emit(kind, model):
    if kind == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        print("not json at all")
        print(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 20,
            "output_tokens": 30}}))
    elif kind == "claude":
        print(json.dumps({
            "type": "result", "is_error": False, "result": "done",
            "total_cost_usd": 0.0123, "session_id": "s1",
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                      "cache_read_input_tokens": 85, "output_tokens": 30},
            "modelUsage": {"claude-haiku-5-5": {"outputTokens": 30},
                           "claude-other": {"outputTokens": 1}}}))
    elif kind == "dsh":
        print(json.dumps({"type": "session", "session_id": "s1"}))
        print(json.dumps({"type": "thinking", "text": "working"}))
        print(json.dumps({"type": "status", "phase": "turn_end",
                          "turn": 1,
                          "reason": {"kind": "completed"},
                          "usage": {"prompt_tokens": 70,
                                    "completion_tokens": 9}}))
        print(json.dumps({"type": "final", "text": "done"}))
    elif kind == "dsh_incomplete":
        print(json.dumps({"type": "final", "text": "done"}))
    elif kind == "garbage":
        print("<<< totally not json >>>")
    else:
        raise SystemExit(f"unknown emit {kind}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--emit", required=True)
    parser.add_argument("--ws", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--bench", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--seen-dir", default=None)
    args = parser.parse_args()

    stdin_text = sys.stdin.read()
    # 记录执行器启动时看到的工作区，测试据此确认没有泄露评测材料。
    if args.seen_dir:
        ws = Path(args.ws)
        seen = {
            "cwd": os.getcwd(),
            "files": sorted(p.relative_to(ws).as_posix()
                            for p in ws.rglob("*") if p.is_file()),
            "stdin": stdin_text,
            "prompt": args.prompt,
            "model": args.model,
        }
        os.makedirs(args.seen_dir, exist_ok=True)
        name = f"{args.task}_{int(time.time() * 1e6)}.json"
        (Path(args.seen_dir) / name).write_text(
            json.dumps(seen, ensure_ascii=False), encoding="utf-8")

    code = apply_mode(args.mode, args.bench, args.task, args.ws)
    emit(args.emit, args.model)
    return code


if __name__ == "__main__":
    sys.exit(main())
