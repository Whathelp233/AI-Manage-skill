#!/usr/bin/env python3
"""py_score.py：Python 题 hidden/score.sh 共用的评分流程（对应 score_lib.sh）。

在工作区根目录执行，例如 hidden/score.sh 里写：

    exec python3 hidden/common/py_score.py \\
        --groups "regression:8 feature:5 rootcause:4" \\
        --tests hidden/test_regression.py hidden/test_feature.py \\
        --compile pipeline.py lc_common --jobs 3 --timeout 300

参数：
  --groups   "组名:用例数 ..."，顺序即输出顺序；必须与隐藏测试里的用例数一致。
  --tests    隐藏测试模块（相对工作区根），模块里用 py_mt.case 登记用例。
  --compile  先做语法检查的工作区文件或目录（只编译不写 .pyc）；有语法错误
             时按“编译失败”计 0 分、compile_ok 为 false。
  --jobs     并行运行的用例数，默认 2。
  --timeout  单用例超时秒数，默认 120。
  --python   运行用例的解释器；默认取环境变量 BENCH_PYTHON，否则当前解释器。

计分：每个用例单独起子进程运行，返回 0 为通过；异常、崩溃、超时都只影响
这一个用例。

诊断模式：环境变量 BENCH_ONLY_TESTS 给出测试模块时（空格分隔），只运行
这些模块、不核对 --groups，分组按实际用例输出。selfcheck 用它验证 B 类的
regression 用例在 starter 上全部通过。

输出：过程信息在前，stdout 最后一行是 INTERFACE.md 规定的 JSON。退出码：
正常评分一律 0；只有评分脚本自身出错（声明的用例数与实际不符等）才非 0。
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

BUILD_DIR = Path("hidden/build")
HERE = Path(__file__).resolve().parent


def die(message):
    print(f"py_score 错误: {message}", file=sys.stderr)
    sys.exit(2)


def default_python():
    return os.environ.get("BENCH_PYTHON") or sys.executable


def load_module(path):
    path = Path(path).resolve()
    for entry in (str(HERE), str(path.parent)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(
        "bench_case_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import py_mt  # noqa: E402  (路径在上面加入)
    return py_mt


def child_list(files):
    import_count = 0
    for file in files:
        py_mt = load_module(file)
        for group, name, _ in py_mt.CASES[import_count:]:
            print(f"{group}\t{name}\t{file}")
        import_count = len(py_mt.CASES)
    return 0


def child_run(file, name):
    py_mt = load_module(file)
    for _, case_name, function in py_mt.CASES:
        if case_name == name:
            function()
            print(f"[PASS] {name}")
            return 0
    print(f"没有用例 {name}", file=sys.stderr)
    return 3


COMPILE_SNIPPET = r"""
import sys
from pathlib import Path
bad = 0
for root in sys.argv[1:]:
    root = Path(root)
    files = [root] if root.is_file() else sorted(root.rglob('*.py'))
    for path in files:
        try:
            compile(path.read_text(encoding='utf-8'), str(path), 'exec')
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            print(f'{path}: {exc}')
            bad = 1
sys.exit(bad)
"""


def emit(order, passed, total, compile_ok):
    parts = ", ".join(f'"{g}": [{passed[g]}, {total[g]}]' for g in order)
    print('{"passed": %d, "total": %d, "groups": {%s}, "compile_ok": %s}' % (
        sum(passed[g] for g in order), sum(total[g] for g in order), parts,
        "true" if compile_ok else "false"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", default="")
    parser.add_argument("--tests", nargs="*", default=[])
    parser.add_argument("--compile", nargs="*", default=[])
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--python", default=None)
    parser.add_argument("--list", nargs="*")
    parser.add_argument("--run", nargs=2, metavar=("FILE", "NAME"))
    args = parser.parse_args()
    if args.list is not None:
        return child_list(args.list)
    if args.run:
        return child_run(*args.run)

    python = args.python or default_python()
    only = os.environ.get("BENCH_ONLY_TESTS", "").split()
    diagnostic = bool(only)
    tests = only or args.tests
    if not tests:
        die("没有给出 --tests")
    order, passed, total = [], {}, {}
    for entry in args.groups.split():
        group, _, count = entry.partition(":")
        order.append(group)
        passed[group] = 0
        total[group] = int(count)
    if not diagnostic and not order:
        die("没有给出 --groups")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    logs = BUILD_DIR / "logs"
    logs.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    if args.compile:
        targets = [p for p in args.compile if Path(p).exists()]
        missing = [p for p in args.compile if not Path(p).exists()]
        proc = subprocess.run([python, "-c", COMPILE_SNIPPET, *targets],
                              capture_output=True, text=True, env=env)
        if missing or proc.returncode != 0:
            print("编译失败（语法检查），节选：")
            print(("缺少 " + " ".join(missing) + "\n" if missing else "")
                  + proc.stdout[-2000:] + proc.stderr[-1000:])
            if diagnostic:
                order = []
            emit(order, passed, total, False)
            return 0

    proc = subprocess.run(
        [python, str(HERE / "py_score.py"), "--list", *tests],
        capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        print("隐藏测试无法列出用例，全部判失败：")
        print(proc.stderr[-2000:])
        if diagnostic:
            order = []
        emit(order, passed, total, True)
        return 0
    cases = [line.split("\t") for line in proc.stdout.splitlines()
             if line.count("\t") == 2]
    actual = {}
    for group, _, _ in cases:
        actual[group] = actual.get(group, 0) + 1
    if diagnostic:
        order, passed, total = [], {}, {}
        for group, _, _ in cases:
            if group not in total:
                order.append(group)
                passed[group] = 0
                total[group] = actual[group]
    else:
        for group in order:
            if actual.get(group, 0) != total[group]:
                die(f"组 {group} 声明 {total[group]} 项，实际 "
                    f"{actual.get(group, 0)} 项")
        for group in actual:
            if group not in total:
                die(f"隐藏测试里的组 {group} 没有在 --groups 中声明")

    def run_case(item):
        group, name, file = item
        log = logs / f"{group}.{name}.log"
        try:
            with open(log, "w", encoding="utf-8") as out:
                rc = subprocess.run(
                    [python, str(HERE / "py_score.py"), "--run", file, name],
                    stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env,
                    timeout=args.timeout).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        return group, name, rc, log

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(run_case, cases))
    for group, name, rc, log in results:
        if rc == 0:
            passed[group] += 1
            continue
        print(f"FAIL {group}.{name} (rc={rc})")
        text = log.read_text(encoding="utf-8", errors="replace")
        for line in text.strip().splitlines()[-6:]:
            print("    " + line[:240])
    emit(order, passed, total, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
