#!/usr/bin/env python3
"""执行器模型准入基准：自检、运行、汇总、判定。

子命令：
    selfcheck  对每道题检查 reference 满分、starter 明显不满分等出题约束
    run        让某个执行器模型做题，记录过程维度并评分
    report     按类别、划分和模型汇总结果
    judge      按 docs/DESIGN.md 6.2 判定候选模型能否进入某类路由

题目接口见 bench/INTERFACE.md，使用说明见 bench/README.md。
只用标准库和 PyYAML。
"""
import argparse
import concurrent.futures
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment error
    raise SystemExit("error: PyYAML is required (pip install PyYAML)") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import style_check  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent
SOURCE_LAYOUT = MODULE_DIR.name == "tools"
ROOT = MODULE_DIR.parent if SOURCE_LAYOUT else MODULE_DIR
DEFAULT_BENCH = ROOT / "bench"
DEFAULT_CONFIG = (ROOT / "config" / "models.yaml" if SOURCE_LAYOUT else
                  ROOT.parent / "model-routing" / "config" / "models.yaml")
DEFAULT_EXECUTOR_MD = (ROOT / "executor" / "EXECUTOR.md" if SOURCE_LAYOUT
                       else ROOT.parent / "executor-dispatch" / "EXECUTOR.md")
META_FIELDS = ("id", "category", "split", "language", "score_cmd",
               "timeout_s", "total_checks", "groups", "summary",
               "allowed_paths", "self_test_cmd", "starter_tests")
CATEGORIES = ("A", "B", "C")
SPLITS = ("dev", "holdout")
LANGUAGES = ("cpp", "python")
EXECUTORS = ("codex", "claude", "dsh")
# 被测模型的工作区里绝不能出现的东西。
FORBIDDEN_IN_WORKSPACE = ("hidden", "reference", "TASKS.md", "meta.yaml")
# C 类题必须有的分组。
C_REQUIRED_GROUPS = ("safety", "asan", "tsan")
# C 类的安全组：判定时每次运行里这些组（有的话）都必须 100% 通过。
# concurrency 组测的是回调顺序、死锁这类并发安全性质，同样算安全用例。
SAFETY_GROUPS = ("safety", "concurrency", "asan", "tsan")
STYLE_DIRS = ("include", "src", "test")
TEST_DIR = "test"
TEST_SUFFIXES = (".cpp", ".cc", ".h", ".hpp", ".py")
STARTER_MAX_RATIO = 0.5
B_STARTER_MIN_LINES = 800
REPORT_CLAIMS = ("PASS", "FAIL", "NOT_RUN")
REPORT_LINE = re.compile(r"^TESTS: (PASS|FAIL|NOT_RUN)$")
DSH_MODEL_ID = "deepseek-flash"

# 执行器命令模板。占位符：{ws} 工作区，{model} 模型，{prompt} task.md 全文，
# {prompt_file} 写有 task.md 全文的文件（在工作区外），{last} 最后一条消息
# 的输出文件（在工作区外），{task} 题目 id，{rep} 第几次。
EXECUTOR_TEMPLATES = {
    "codex": ("codex exec -C {ws} -m {model} "
              "-c model_reasoning_effort=\"medium\" -s workspace-write "
              "--skip-git-repo-check --json -o {last} {prompt}"),
    "claude": ("claude -p --model {model} --output-format json "
              "--permission-mode auto --setting-sources project {prompt}"),
    "dsh": "~/.local/bin/dsh-exec {ws} --json -",
}
# 执行器输出里出现这些字样，说明它可能读了工作区以外的评测材料。
LEAK_PATTERNS = ("bench/tasks", "TASKS.md", "/hidden/", "hidden/score",
                 "/reference/", "meta.yaml")

CASE_PATTERN = re.compile(
    r"\b(?:MT_TEST|TEST|TEST_F|TEST_P|TEST_CASE|SECTION)\s*\("
    r"|\b(?:void|bool|int|def)\s+(?:test\w*|\w+Test\w*)\s*\(")
ASSERT_PATTERN = re.compile(
    r"\b\w*(?:assert|expect|check|require|verify)\w*\s*\(", re.IGNORECASE)


class BenchError(Exception):
    pass


# --------------------------------------------------------------- 题目加载

def _is_str_list(value, allow_empty=False):
    return (isinstance(value, list) and (allow_empty or value)
            and all(isinstance(x, str) and x for x in value)
            and len(set(value)) == len(value))


def load_meta(task_dir):
    """读取并校验 meta.yaml，返回字典。"""
    task_dir = Path(task_dir)
    path = task_dir / "meta.yaml"
    name = task_dir.name
    try:
        with open(path, encoding="utf-8") as f:
            meta = yaml.safe_load(f)
    except OSError as e:
        raise BenchError(f"{name}: 读不到 meta.yaml: {e}")
    except yaml.YAMLError as e:
        raise BenchError(f"{name}: meta.yaml 不是合法 YAML: {e}")
    if not isinstance(meta, dict):
        raise BenchError(f"{name}: meta.yaml 必须是映射")
    missing = [k for k in META_FIELDS if k not in meta]
    extra = [k for k in meta if k not in META_FIELDS]
    if missing or extra:
        raise BenchError(
            f"{name}: meta.yaml 字段不符（缺 {missing}，多 {extra}）")
    if meta["id"] != name:
        raise BenchError(f"{name}: meta.id 与目录名不同")
    if meta["category"] not in CATEGORIES:
        raise BenchError(f"{name}: category 必须是 A/B/C")
    if meta["split"] not in SPLITS:
        raise BenchError(f"{name}: split 必须是 dev/holdout")
    if meta["language"] not in LANGUAGES:
        raise BenchError(f"{name}: language 必须是 cpp/python")
    for key in ("score_cmd", "self_test_cmd", "summary"):
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise BenchError(f"{name}: {key} 必须是非空字符串")
    for key in ("timeout_s", "total_checks"):
        if type(meta[key]) is not int or meta[key] <= 0:
            raise BenchError(f"{name}: {key} 必须是正整数")
    if not _is_str_list(meta["groups"]):
        raise BenchError(f"{name}: groups 必须是不重复的名称列表")
    if not _is_str_list(meta["allowed_paths"]):
        raise BenchError(f"{name}: allowed_paths 必须是非空的 glob 列表")
    if not _is_str_list(meta["starter_tests"], allow_empty=True):
        raise BenchError(f"{name}: starter_tests 必须是路径列表")
    return meta


def list_task_dirs(bench_dir):
    tasks_dir = Path(bench_dir) / "tasks"
    if not tasks_dir.is_dir():
        raise BenchError(f"找不到题目目录 {tasks_dir}")
    return sorted(p for p in tasks_dir.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def select_tasks(bench_dir, only=None, split=None, category=None):
    """返回 [(task_dir, meta)]，按 id 排序。"""
    task_dirs = list_task_dirs(bench_dir)
    if only:
        known = {p.name for p in task_dirs}
        unknown = [t for t in only if t not in known]
        if unknown:
            raise BenchError(f"没有这些题: {', '.join(unknown)}")
        task_dirs = [p for p in task_dirs if p.name in only]
    tasks = []
    for task_dir in task_dirs:
        meta = load_meta(task_dir)
        if split and meta["split"] != split:
            continue
        if category and meta["category"] != category:
            continue
        tasks.append((task_dir, meta))
    return tasks


# --------------------------------------------------------------- 工作区与评分

def copy_tree_contents(src, dst):
    """把 src 目录的内容拷进 dst（dst 可已存在）。"""
    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)


def remove_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def add_hidden(task_dir, bench_dir, workspace):
    """评分前把 hidden/ 和公共框架放进工作区，覆盖同名内容。"""
    hidden = Path(workspace) / "hidden"
    remove_path(hidden)
    shutil.copytree(Path(task_dir) / "hidden", hidden)
    common = Path(bench_dir) / "common"
    if common.is_dir():
        shutil.copytree(common, hidden / "common", dirs_exist_ok=True)


def last_json_line(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise BenchError("没有输出")
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as e:
        raise BenchError(f"最后一行不是 JSON: {e}")
    if not isinstance(data, dict):
        raise BenchError("最后一行的 JSON 必须是对象")
    return data


def parse_score_output(stdout):
    """取 stdout 最后一个非空行并校验评分 JSON；不合法抛 BenchError。"""
    data = last_json_line(stdout)
    if set(data) != {"passed", "total", "groups", "compile_ok"}:
        raise BenchError(f"评分 JSON 的键不符: {sorted(data)}")
    if type(data["passed"]) is not int or type(data["total"]) is not int:
        raise BenchError("passed / total 必须是整数")
    if type(data["compile_ok"]) is not bool:
        raise BenchError("compile_ok 必须是布尔值")
    groups = data["groups"]
    if not isinstance(groups, dict):
        raise BenchError("groups 必须是对象")
    for name, pair in groups.items():
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(x) is not int for x in pair)
                or not 0 <= pair[0] <= pair[1]):
            raise BenchError(f"groups.{name} 必须是 [passed, total]")
    if not 0 <= data["passed"] <= data["total"]:
        raise BenchError("passed 必须在 0 和 total 之间")
    if groups and (sum(p for p, _ in groups.values()) != data["passed"]
                   or sum(t for _, t in groups.values()) != data["total"]):
        raise BenchError("groups 的合计与 passed / total 不一致")
    return data


def run_shell(command, cwd, timeout_s, env_extra=None):
    """执行 shell 命令，返回 (returncode 或 None 表示超时, 输出)。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors="replace",
        start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # 连同子进程一起杀掉，否则残留的子进程会占着管道。
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        return None, out or ""
    return proc.returncode, out


def run_score(meta, workspace, env_extra=None):
    """在工作区根目录执行 score_cmd。

    返回 (score, stdout, error)：成功时 error 为 None；评分脚本自身出错
    （非 0 退出、超时、输出不合法）时 score 为 None。
    """
    rc, out = run_shell(meta["score_cmd"], workspace, meta["timeout_s"],
                        env_extra)
    if rc is None:
        return None, out, f"评分超时（{meta['timeout_s']} s）"
    if rc != 0:
        return None, out, f"score_cmd 退出码 {rc}"
    try:
        return parse_score_output(out), out, None
    except BenchError as e:
        return None, out, f"评分输出不合法: {e}"


def score_tree(task_dir, meta, bench_dir, source, workspace, env_extra=None):
    """把 source 拷成工作区，加入 hidden 后评分。"""
    copy_tree_contents(source, workspace)
    add_hidden(task_dir, bench_dir, workspace)
    return run_score(meta, workspace, env_extra)


def run_style(workspace):
    """对工作区里的 include/ src/ test/ 跑规范检查，返回精简结果。"""
    paths = [str(Path(workspace) / d) for d in STYLE_DIRS
             if (Path(workspace) / d).is_dir()]
    result = style_check.run_checks(paths)
    return {
        "passed": result["passed"],
        "total": result["total"],
        "ratio": result["ratio"],
        "files": result["files"],
        "failed": sorted(k for k, v in result["checks"].items()
                         if not v["ok"]),
    }


# --------------------------------------------------------------- 过程维度

def glob_to_regex(pattern):
    """把 allowed_paths 的 glob 转成正则：** 跨目录，* 和 ? 不跨目录。"""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if pattern.startswith("/", i) and len(out) == 1:
                # 开头的 **/ 也要能匹配根目录下的文件。
                out[-1] = "(?:.*/)?"
                i += 1
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_allowed(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


def list_files(root):
    """返回 root 下全部文件的相对路径集合（POSIX 分隔符）。"""
    root = Path(root)
    files = set()
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in names:
            files.add((Path(base) / name).relative_to(root).as_posix())
        for name in list(dirs):
            if (Path(base) / name).is_symlink():
                files.add((Path(base) / name).relative_to(root).as_posix())
    return files


def same_content(a, b):
    a, b = Path(a), Path(b)
    if a.is_symlink() or b.is_symlink():
        return (a.is_symlink() and b.is_symlink()
                and os.readlink(a) == os.readlink(b))
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def count_test_items(path):
    """尽力统计一个测试文件里的 (用例数, 断言数)。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    if str(path).endswith((".cpp", ".cc", ".h", ".hpp")):
        text = style_check.strip_code(text)
    return len(CASE_PATTERN.findall(text)), len(ASSERT_PATTERN.findall(text))


def parse_report_claim(workspace):
    """REPORT.md 第一行的声明：PASS / FAIL / NOT_RUN，缺失或格式错为 missing。"""
    path = Path(workspace) / "REPORT.md"
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            first = f.readline()
    except OSError:
        return "missing"
    match = REPORT_LINE.match(first.rstrip("\r\n").rstrip())
    return match.group(1) if match else "missing"


def diff_workspace(task_dir, meta, workspace, with_task_md=True):
    """对比工作区与 starter，返回 (tests_written, scope_violations)。

    runner 建的工作区里还有一份 task.md，它也算基线；selfcheck 直接拿
    reference/ 目录来比时没有这个文件，with_task_md 传 False。
    """
    starter = Path(task_dir) / "starter"
    workspace = Path(workspace)
    before = list_files(starter)
    baseline_extra = {"task.md"} if with_task_md else set()
    after = list_files(workspace)
    violations = []

    def baseline_path(rel):
        return (Path(task_dir) / rel) if rel in baseline_extra \
            else (starter / rel)

    baseline = before | baseline_extra
    changed = []
    for rel in sorted(after - baseline):
        changed.append((rel, "新增"))
    for rel in sorted(baseline - after):
        changed.append((rel, "删除"))
    for rel in sorted(after & baseline):
        if not same_content(baseline_path(rel), workspace / rel):
            changed.append((rel, "修改"))
    for rel, kind in changed:
        if not path_allowed(rel, meta["allowed_paths"]):
            violations.append(
                {"path": rel, "reason": f"{kind}了 allowed_paths 以外的文件"})

    for rel in meta["starter_tests"]:
        if rel not in after:
            violations.append(
                {"path": rel, "reason": "删除了 starter 自带的测试文件"})
            continue
        cases_before, asserts_before = count_test_items(starter / rel)
        cases_after, asserts_after = count_test_items(workspace / rel)
        if cases_after < cases_before:
            violations.append({
                "path": rel,
                "reason": f"starter 测试的用例数减少（{cases_before} → "
                          f"{cases_after}）"})
        if asserts_after < asserts_before:
            violations.append({
                "path": rel,
                "reason": f"starter 测试的断言数减少（{asserts_before} → "
                          f"{asserts_after}）"})

    tests_written = False
    prefix = TEST_DIR + "/"
    for rel in sorted(after):
        if not rel.startswith(prefix) or not rel.endswith(TEST_SUFFIXES):
            continue
        if rel not in before:
            if sum(count_test_items(workspace / rel)) > 0:
                tests_written = True
            continue
        cases_before, asserts_before = count_test_items(starter / rel)
        cases_after, asserts_after = count_test_items(workspace / rel)
        if cases_after > cases_before or asserts_after > asserts_before:
            tests_written = True
    return tests_written, violations


def run_self_test(meta, bench_dir, workspace, scratch):
    """在工作区的副本里运行 self_test_cmd，返回结果字典。

    正常时为 {"compile_ok", "passed", "total"}；命令本身出错（超时、非 0
    退出、输出不合法）时为 {"error": 原因}。
    """
    scratch = Path(scratch)
    remove_path(scratch)
    copy_tree_contents(workspace, scratch)
    remove_path(scratch / "build")
    env = {"BENCH_COMMON": str((Path(bench_dir) / "common").resolve()),
           "SELF_TEST_TIMEOUT_S": str(max(10, meta["timeout_s"] - 30))}
    rc, out = run_shell(meta["self_test_cmd"], scratch, meta["timeout_s"],
                        env)
    log = out[-4000:]
    if rc is None:
        return {"error": f"self_test 超时（{meta['timeout_s']} s）"}, log
    if rc != 0:
        return {"error": f"self_test_cmd 退出码 {rc}"}, log
    try:
        data = last_json_line(out)
    except BenchError as e:
        return {"error": f"self_test 输出不合法: {e}"}, log
    if (set(data) != {"compile_ok", "passed", "total"}
            or type(data["compile_ok"]) is not bool
            or type(data["passed"]) is not int
            or type(data["total"]) is not int
            or not 0 <= data["passed"] <= data["total"]):
        return {"error": "self_test 输出的 JSON 形状不符"}, log
    return data, log


def self_test_passed(self_test):
    """self_test 是否全部通过；结果不可用时返回 None。"""
    if "error" in self_test:
        return None
    return (self_test["compile_ok"] and self_test["total"] > 0
            and self_test["passed"] == self_test["total"])


def judge_honesty(report_claim, self_test):
    """按 INTERFACE.md：声称 PASS 但自测不过为 False；缺报告为 None。

    self_test 本身跑不出结果（超时、脚本出错）时无法判断，也记 None。
    """
    if report_claim == "missing":
        return None
    if report_claim != "PASS":
        return True
    return self_test_passed(self_test)


def compute_process(task_dir, meta, bench_dir, workspace, scratch,
                    with_task_md=True):
    """计算过程维度，返回 (字典, self_test 日志)。"""
    tests_written, violations = diff_workspace(task_dir, meta, workspace,
                                               with_task_md)
    report_claim = parse_report_claim(workspace)
    self_test, log = run_self_test(meta, bench_dir, workspace, scratch)
    return {
        "tests_written": tests_written,
        "scope_violations": violations,
        "report_claim": report_claim,
        "self_test": self_test,
        "honest": judge_honesty(report_claim, self_test),
    }, log


# --------------------------------------------------------------- selfcheck

def count_source_lines(root):
    total = 0
    for sub in ("include", "src"):
        base = Path(root) / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix in (".h", ".cpp") and path.is_file():
                with open(path, encoding="utf-8", errors="replace") as f:
                    total += sum(1 for _ in f)
    return total


def strict_compile(root):
    """用 -Wall -Wextra -Werror 对 src 下的 .cpp 做语法检查，返回错误文本。"""
    sources = sorted(str(p) for p in (Path(root) / "src").rglob("*.cpp"))
    if not sources:
        return "src/ 下没有 .cpp"
    proc = subprocess.run(
        ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread",
         "-fsyntax-only", "-I", str(Path(root) / "include")] + sources,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors="replace")
    return None if proc.returncode == 0 else proc.stdout[:2000]


def selfcheck_task(task_dir, bench_dir, tmp_root, repeat):
    """检查一道题，返回 (问题列表, 信息字典)。"""
    problems = []
    info = {"id": task_dir.name}
    try:
        meta = load_meta(task_dir)
    except BenchError as e:
        return [str(e)], info
    info["category"] = meta["category"]
    info["split"] = meta["split"]
    for name in ("task.md", "starter", "reference", "hidden"):
        if not (task_dir / name).exists():
            problems.append(f"缺少 {name}")
    if problems:
        return problems, info
    for name in FORBIDDEN_IN_WORKSPACE:
        if (task_dir / "starter" / name).exists():
            problems.append(f"starter/ 里不能有 {name}")
    task_text = (task_dir / "task.md").read_text(encoding="utf-8")
    for word in ("hidden/", "reference/", "TASKS.md", "隐藏测试"):
        if word in task_text:
            problems.append(f"task.md 提到了 {word}")
    for word in ("TESTS: PASS", "TESTS: FAIL", "TESTS: NOT_RUN", "test/"):
        if word not in task_text:
            problems.append(f"task.md 没有提到 {word}（测试与报告要求）")
    if not meta["starter_tests"]:
        problems.append("starter_tests 为空：starter 必须自带测试骨架")
    for rel in meta["starter_tests"]:
        if not (task_dir / "starter" / rel).is_file():
            problems.append(f"starter_tests 里的 {rel} 不存在")
        elif not path_allowed(rel, meta["allowed_paths"]):
            problems.append(f"starter_tests 里的 {rel} 不在 allowed_paths 内")

    work = Path(tempfile.mkdtemp(prefix=task_dir.name + "_", dir=tmp_root))
    try:
        # reference：必须满分，可重复多次查偶发失败。
        for i in range(repeat):
            ws = work / f"reference_{i}"
            score, out, err = score_tree(task_dir, meta, bench_dir,
                                         task_dir / "reference", ws)
            if err:
                problems.append(f"reference 评分出错: {err}")
                info["reference_tail"] = out[-1500:]
                break
            info["reference"] = f"{score['passed']}/{score['total']}"
            if not score["compile_ok"]:
                problems.append("reference 编译失败")
            if score["passed"] != score["total"]:
                problems.append(
                    f"reference 不是满分: {info['reference']}"
                    f"（第 {i + 1} 次）")
                info["reference_tail"] = out[-1500:]
            if score["total"] != meta["total_checks"]:
                problems.append(
                    f"reference total {score['total']} 与 meta.total_checks "
                    f"{meta['total_checks']} 不同")
            if list(score["groups"]) != meta["groups"]:
                problems.append(
                    f"评分输出的 groups {list(score['groups'])} 与 meta.groups "
                    f"{meta['groups']} 不同")
            if problems:
                break

        # starter：必须明显不满分，JSON 仍要合法。
        ws = work / "starter"
        score, out, err = score_tree(task_dir, meta, bench_dir,
                                     task_dir / "starter", ws)
        if err:
            problems.append(f"starter 评分出错: {err}")
            info["starter_tail"] = out[-1500:]
        else:
            info["starter"] = f"{score['passed']}/{score['total']}"
            info["starter_compile_ok"] = score["compile_ok"]
            if score["total"] != meta["total_checks"]:
                problems.append(
                    f"starter total {score['total']} 与 meta.total_checks "
                    f"{meta['total_checks']} 不同")
            if score["passed"] > STARTER_MAX_RATIO * score["total"]:
                problems.append(
                    f"starter 得分 {info['starter']} 过高（上限 "
                    f"{STARTER_MAX_RATIO:.0%}），区分度不足")
            if list(score["groups"]) != meta["groups"]:
                problems.append("starter 评分输出的 groups 与 meta.groups 不同")

        # 过程维度：reference 当作理想答案，starter 的测试骨架要能运行。
        process, log = compute_process(
            task_dir, meta, bench_dir, task_dir / "reference",
            work / "reference_selftest", with_task_md=False)
        info["reference_process"] = (
            f"claim={process['report_claim']},"
            f"honest={process['honest']},"
            f"tests_written={process['tests_written']},"
            f"violations={len(process['scope_violations'])}")
        if self_test_passed(process["self_test"]) is not True:
            problems.append(
                f"reference 的 self_test 没通过: {process['self_test']}")
            info["reference_selftest_tail"] = log[-1500:]
        if process["scope_violations"]:
            problems.append(
                f"reference 违反 scope 规则: {process['scope_violations']}")
        if process["report_claim"] != "PASS":
            problems.append("reference/REPORT.md 第一行必须是 TESTS: PASS")
        if process["honest"] is not True:
            problems.append("reference 的 honest 不是 true")
        if not process["tests_written"]:
            problems.append("reference 没有比 starter 多写测试")
        self_test, log = run_self_test(
            meta, bench_dir, task_dir / "starter", work / "starter_selftest")
        info["starter_self_test"] = (
            "error" if "error" in self_test else
            f"compile_ok={self_test['compile_ok']},"
            f"{self_test['passed']}/{self_test['total']}")
        if "error" in self_test or not self_test["compile_ok"]:
            problems.append(
                f"starter 的测试骨架不能编译运行: {self_test}")
            info["starter_selftest_tail"] = log[-1500:]

        if meta["language"] == "cpp":
            for name in ("starter", "reference"):
                error = strict_compile(task_dir / name)
                if error:
                    problems.append(
                        f"{name} 不能在 -Wall -Wextra -Werror 下编译: "
                        f"{error[:300]}")
                style = style_check.run_checks(
                    [str(task_dir / name / d) for d in STYLE_DIRS
                     if (task_dir / name / d).is_dir()])
                info[f"{name}_style"] = f"{style['passed']}/{style['total']}"
                if style["passed"] != style["total"]:
                    bad = {k: v["examples"][:2]
                           for k, v in style["checks"].items() if not v["ok"]}
                    problems.append(f"{name} 规范检查未全过: {bad}")

        if meta["category"] == "B":
            lines = count_source_lines(task_dir / "starter")
            info["starter_lines"] = lines
            if lines < B_STARTER_MIN_LINES:
                problems.append(
                    f"B 类 starter 只有 {lines} 行（要求 ≥ "
                    f"{B_STARTER_MIN_LINES}）")
            if "regression" not in meta["groups"]:
                problems.append("B 类必须有 regression 组")
            regression = task_dir / "hidden" / "test_regression.cpp"
            if not regression.is_file():
                problems.append("B 类必须有 hidden/test_regression.cpp")
            else:
                # regression 用例只依赖旧接口，在 starter 上必须全过。
                ws = work / "starter_regression"
                score, out, err = score_tree(
                    task_dir, meta, bench_dir, task_dir / "starter", ws,
                    {"BENCH_ONLY_TESTS": "hidden/test_regression.cpp"})
                if err:
                    problems.append(f"starter regression 评分出错: {err}")
                else:
                    pair = score["groups"].get("regression", [0, 0])
                    info["starter_regression"] = f"{pair[0]}/{pair[1]}"
                    if (not score["compile_ok"] or pair[0] != pair[1]
                            or pair[1] == 0):
                        problems.append(
                            "regression 用例在 starter 上没有全过: "
                            f"{info['starter_regression']}")
                        info["starter_regression_tail"] = out[-1500:]
            if self_test_passed(self_test) is not True:
                problems.append("B 类 starter 自带的测试必须全部通过")

        if meta["category"] == "C":
            for group in C_REQUIRED_GROUPS:
                if group not in meta["groups"]:
                    problems.append(f"C 类必须有 {group} 组")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return problems, info


def cmd_selfcheck(args):
    bench_dir = Path(args.bench_dir)
    task_dirs = list_task_dirs(bench_dir)
    if args.tasks:
        known = {p.name for p in task_dirs}
        unknown = [t for t in args.tasks if t not in known]
        if unknown:
            raise BenchError(f"没有这些题: {', '.join(unknown)}")
        task_dirs = [p for p in task_dirs if p.name in args.tasks]
    if not task_dirs:
        raise BenchError("没有可检查的题目")
    tmp_root = args.tmp or tempfile.gettempdir()
    os.makedirs(tmp_root, exist_ok=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(
            lambda d: selfcheck_task(d, bench_dir, tmp_root, args.repeat),
            task_dirs))

    failed = 0
    keys = ("category", "split", "reference", "starter",
            "starter_compile_ok", "starter_regression", "starter_lines",
            "reference_style", "starter_style", "reference_process",
            "starter_self_test")
    for problems, info in results:
        status = "OK  " if not problems else "FAIL"
        failed += 1 if problems else 0
        parts = [f"{k}={info[k]}" for k in keys if k in info]
        print(f"[{status}] {info['id']}: {' '.join(parts)}")
        for problem in problems:
            print(f"       - {problem}")
        if problems and args.verbose:
            for key in sorted(info):
                if key.endswith("_tail"):
                    print(f"       --- {key} ---")
                    for line in str(info[key]).splitlines():
                        print(f"       | {line}")
    total = len(results)
    print(f"selfcheck: {total - failed}/{total} 题通过")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------- 执行器

def find_usage_dicts(obj, found):
    """递归收集形如 {"input_tokens": .., "output_tokens": ..} 的字典。"""
    if isinstance(obj, dict):
        keys = set(obj)
        if ({"input_tokens", "output_tokens"} <= keys
                or {"prompt_tokens", "completion_tokens"} <= keys):
            found.append(obj)
        for value in obj.values():
            find_usage_dicts(value, found)
    elif isinstance(obj, list):
        for value in obj:
            find_usage_dicts(value, found)


def usage_tokens(usage):
    """从一个 usage 字典取 (输入 tokens, 输出 tokens)；输入含缓存部分。"""
    def number(*names):
        total = None
        for name in names:
            value = usage.get(name)
            if type(value) in (int, float):
                total = (total or 0) + value
        return total
    if "input_tokens" in usage:
        tokens_in = number("input_tokens", "cache_creation_input_tokens",
                           "cache_read_input_tokens")
        tokens_out = number("output_tokens")
    else:
        tokens_in = number("prompt_tokens")
        tokens_out = number("completion_tokens")
    return tokens_in, tokens_out


def parse_jsonl(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def sum_event_usage(events, event_types):
    """累加指定类型事件里的 usage；没有则退回到最后一个 usage 字典。"""
    tokens_in = tokens_out = None
    for event in events:
        if event.get("type") in event_types:
            found = []
            find_usage_dicts(event, found)
            if found:
                t_in, t_out = usage_tokens(found[0])
                if t_in is not None:
                    tokens_in = (tokens_in or 0) + t_in
                if t_out is not None:
                    tokens_out = (tokens_out or 0) + t_out
    if tokens_in is None and tokens_out is None:
        found = []
        find_usage_dicts(events, found)
        if found:
            tokens_in, tokens_out = usage_tokens(found[-1])
    return tokens_in, tokens_out


def parse_executor_output(executor, model, exit_code, stdout):
    """从执行器输出里取 tokens、花费、实际模型、是否成功。解析失败记 None。"""
    info = {"tokens_in": None, "tokens_out": None, "cost_usd": None,
            "model_actual": None, "executor_ok": exit_code == 0}
    try:
        if executor == "codex":
            events = parse_jsonl(stdout)
            info["tokens_in"], info["tokens_out"] = sum_event_usage(
                events, ("turn.completed",))
            info["model_actual"] = model
        elif executor == "claude":
            data = None
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            if data is None:
                data = json.loads(stdout)
            if isinstance(data, dict):
                usage = data.get("usage")
                if isinstance(usage, dict):
                    info["tokens_in"], info["tokens_out"] = usage_tokens(
                        usage)
                cost = data.get("total_cost_usd")
                if type(cost) in (int, float):
                    info["cost_usd"] = cost
                model_usage = data.get("modelUsage")
                if isinstance(model_usage, dict) and model_usage:
                    def weight(key):
                        entry = model_usage[key]
                        if isinstance(entry, dict):
                            value = entry.get("outputTokens", 0)
                            return value if type(value) in (int, float) else 0
                        return 0
                    info["model_actual"] = max(sorted(model_usage),
                                               key=weight)
                if data.get("is_error") is True:
                    info["executor_ok"] = False
        elif executor == "dsh":
            events = parse_jsonl(stdout)
            completed = any(
                event.get("type") == "status"
                and event.get("phase") == "turn_end"
                and isinstance(event.get("reason"), dict)
                and event["reason"].get("kind") == "completed"
                for event in events)
            info["executor_ok"] = exit_code == 0 and completed
            info["tokens_in"], info["tokens_out"] = sum_event_usage(
                [event for event in events
                 if event.get("type") == "status"
                 and event.get("phase") == "turn_end"], ("status",))
            info["model_actual"] = DSH_MODEL_ID
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return info


def find_leak_hints(text):
    """执行器输出里疑似读取评测材料的片段（最多 5 条）。"""
    hints = []
    for pattern in LEAK_PATTERNS:
        pos = text.find(pattern)
        if pos >= 0:
            snippet = text[max(0, pos - 60):pos + 60].replace("\n", " ")
            hints.append(f"{pattern}: ...{snippet}...")
        if len(hints) >= 5:
            break
    return hints


def build_executor_argv(template, values):
    """把模板拆成 argv 并替换占位符；{prompt} 整体作为一个参数。"""
    argv = []
    for part in shlex.split(template):
        for key, value in values.items():
            part = part.replace("{" + key + "}", value)
        argv.append(os.path.expanduser(part) if part.startswith("~") else part)
    return argv


def run_executor(executor, template, values, rep_dir, timeout_s, env_extra=None,
                 executor_md=None):
    """调用执行器，返回记录字段字典。"""
    workspace = values["ws"]
    stdout_name = "dsh.jsonl" if executor == "dsh" else "executor.stdout"
    stdout_path = Path(rep_dir) / stdout_name
    stderr_path = Path(rep_dir) / "executor.stderr"
    argv = build_executor_argv(template, values)
    if executor == "claude" and executor_md:
        settings = json.dumps({"claudeMdExcludes": [
            str(Path.home() / ".claude" / "CLAUDE.md")]})
        argv.extend(["--append-system-prompt-file", executor_md,
                     "--settings", settings])
    result = {"exit_code": None, "executor_status": "ok", "wall_s": None}
    started = time.monotonic()
    try:
        # dsh 从 stdin 读任务文件；其余执行器的 stdin 接 /dev/null。
        stdin = (open(values["prompt_file"], "rb") if executor == "dsh"
                 else subprocess.DEVNULL)
        try:
            with open(stdout_path, "wb") as out, \
                    open(stderr_path, "wb") as err:
                proc = subprocess.Popen(
                    argv, cwd=workspace, stdin=stdin, stdout=out, stderr=err,
                    env={**os.environ, **(env_extra or {})},
                    start_new_session=True)
                try:
                    result["exit_code"] = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    result["executor_status"] = "timeout"
                    try:
                        os.killpg(proc.pid, 9)
                    except OSError:
                        proc.kill()
                    proc.wait()
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()
    except OSError as e:
        result["executor_status"] = "spawn_error"
        result["executor_error"] = str(e)
    result["wall_s"] = round(time.monotonic() - started, 3)
    try:
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stdout = ""
    parsed = parse_executor_output(executor, values["model"],
                                   result["exit_code"], stdout)
    if result["executor_status"] != "ok":
        parsed["executor_ok"] = False
    elif not parsed["executor_ok"]:
        result["executor_status"] = "error"
    result.update(parsed)
    result["leak_hints"] = find_leak_hints(stdout)
    return result


# --------------------------------------------------------------- run

def read_results(path):
    """读一个 results.jsonl，返回记录列表；坏行报错。"""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    raise BenchError(f"{path}:{line_no} 不是合法 JSON: {e}")
                if isinstance(record, dict):
                    records.append(record)
    except OSError as e:
        raise BenchError(f"读不到 {path}: {e}")
    return records


def existing_holdout_runs(results_root, model):
    """已有的 holdout 结果：{task: [run_id, ...]}。"""
    seen = {}
    root = Path(results_root)
    if not root.is_dir():
        return seen
    for path in sorted(root.glob("*/results.jsonl")):
        for record in read_results(path):
            if record.get("split") == "holdout" and is_model(record, model):
                seen.setdefault(record.get("task"), [])
                if path.parent.name not in seen[record.get("task")]:
                    seen[record.get("task")].append(path.parent.name)
    return seen


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


def run_one(task_dir, meta, bench_dir, args, run_id, work_root, rep,
            template, executor_md_content=None):
    """跑一题的一次重复，返回结果记录。"""
    rep_dir = (Path(work_root) / meta["id"] / safe_name(args.model)
               / str(rep))
    workspace = rep_dir / "ws"
    rep_dir.mkdir(parents=True)
    # 工作区只有 starter 的内容和 task.md。
    shutil.copytree(task_dir / "starter", workspace, symlinks=True)
    shutil.copy2(task_dir / "task.md", workspace / "task.md")
    for name in FORBIDDEN_IN_WORKSPACE:
        if (workspace / name).exists():
            raise BenchError(f"{meta['id']}: 工作区里出现了 {name}")
    prompt = (task_dir / "task.md").read_text(encoding="utf-8")
    if executor_md_content is not None and args.executor == "dsh":
        prompt = executor_md_content + "\n\n---\n\n" + prompt
    prompt_file = rep_dir / "task.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "task": meta["id"],
        "category": meta["category"],
        "split": meta["split"],
        "model": args.model,
        "executor": args.executor,
        "executor_md": args.executor_md_record,
        "executor_md_sha256": args.executor_md_sha256,
        "isolation": (str(work_root / "_codex_home")
                      if args.executor == "codex"
                      and args.executor_md_sha256 is not None
                      else str(args.codex_home) if args.executor == "codex"
                      else "setting-sources=project;outside-home"
                      if args.executor == "claude" else None),
        "rep": rep,
    }
    env_extra = None
    if args.executor == "codex":
        codex_home = args.codex_home
        if args.executor_md_sha256 is not None:
            codex_home = work_root / "_codex_home"
        env_extra = {"CODEX_HOME": str(codex_home)}
    record.update(run_executor(args.executor, template, {
        "ws": str(workspace), "model": args.model, "prompt": prompt,
        "prompt_file": str(prompt_file), "last": str(rep_dir / "last.md"),
        "task": meta["id"], "rep": str(rep),
    }, rep_dir, args.executor_timeout, env_extra=env_extra,
        executor_md=(str(args.executor_md_path)
                     if args.executor == "claude" and
                     args.executor_md_path else None)))

    # 过程维度在拷入 hidden/ 之前计算。
    process, self_test_log = compute_process(
        task_dir, meta, bench_dir, workspace, rep_dir / "selftest")
    record.update(process)
    (rep_dir / "self_test.log").write_text(self_test_log, encoding="utf-8")
    remove_path(rep_dir / "selftest")

    record["style"] = run_style(workspace)

    # 评分在工作区的副本里做，评完删掉副本，不让 hidden/ 留在 work 目录。
    score_dir = rep_dir / "score"
    score, out, err = score_tree(task_dir, meta, bench_dir, workspace,
                                 score_dir)
    (rep_dir / "score.log").write_text(out, encoding="utf-8")
    remove_path(score_dir)
    record["score"] = score
    record["score_error"] = err
    if score is None:
        record["pass_rate"] = None
    else:
        record["pass_rate"] = round(
            score["passed"] / score["total"], 4) if score["total"] else 0.0
    record["safety_ok"] = compute_safety_ok(meta["category"], score)
    record["workspace"] = str(workspace)
    return record


def compute_safety_ok(category, score):
    """C 类一次运行的安全组是否全部满分；不适用或没有评分结果时为 None。

    安全组只对 C 类有意义：A、B 类也有 asan 组，但不按安全用例判定。
    """
    if category != "C" or score is None:
        return None
    safety = [score["groups"][g] for g in SAFETY_GROUPS
              if g in score["groups"]]
    if not safety:
        return None
    return score["compile_ok"] and all(p == t for p, t in safety)


def cmd_run(args):
    bench_dir = Path(args.bench_dir)
    if args.executor_md == "none":
        args.executor_md_path = None
        args.executor_md_content = None
        args.executor_md_record = "none"
        args.executor_md_sha256 = None
    else:
        md_path = Path(args.executor_md)
        if not md_path.is_absolute():
            md_path = ROOT / md_path
        md_path = md_path.resolve()
        if not md_path.is_file():
            raise BenchError(f"executor 说明文件不存在: {md_path}")
        args.executor_md_path = md_path
        args.executor_md_content = md_path.read_text(encoding="utf-8")
        try:
            args.executor_md_record = md_path.relative_to(ROOT).as_posix()
        except ValueError:
            args.executor_md_record = os.path.relpath(md_path, ROOT)
        args.executor_md_sha256 = hashlib.sha256(
            args.executor_md_content.encode("utf-8")).hexdigest()[:12]
    args.work_root = Path(args.work_root or "/tmp/agent-router-work").expanduser()
    if args.executor == "claude":
        home = Path.home().resolve()
        resolved_work_root = args.work_root.resolve()
        try:
            resolved_work_root.relative_to(home)
        except ValueError:
            pass
        else:
            raise BenchError(
                f"claude 工作区必须位于 HOME 之外：Claude Code 会加载祖先目录"
                f"中的 .claude/CLAUDE.md 项目指令（当前 HOME: {home}，"
                f"work_root: {resolved_work_root}）")
    tasks = select_tasks(bench_dir, args.tasks, args.split, args.category)
    if not tasks:
        raise BenchError("没有符合条件的题目")
    if args.reps <= 0:
        raise BenchError("--reps 必须是正整数")
    args.codex_home = Path(args.codex_home).expanduser()
    if args.executor == "codex":
        auth = args.codex_home / "auth.json"
        if not args.codex_home.is_dir():
            raise BenchError(f"codex_home 目录不存在: {args.codex_home}")
        if not auth.is_file():
            raise BenchError(f"codex_home 缺少可读的 auth.json: {auth}")
    results_root = Path(args.results_root or bench_dir / "results")

    holdout = [meta["id"] for _, meta in tasks if meta["split"] == "holdout"]
    if holdout and not args.confirm_holdout:
        raise BenchError(
            "选中了 holdout 题（" + ", ".join(holdout) + "）。holdout 只在最终"
            "判定时跑一次，确认要跑请加 --confirm-holdout")
    if holdout:
        seen = existing_holdout_runs(results_root, args.model)
        clash = {t: seen[t] for t in holdout if t in seen}
        if clash:
            detail = "; ".join(f"{t} 已在 {', '.join(runs)}"
                               for t, runs in sorted(clash.items()))
            raise BenchError(
                f"模型 {args.model} 已有 holdout 结果，拒绝重复运行: {detail}")

    run_id = args.run_id or (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-"
        + safe_name(args.model))
    if safe_name(run_id) != run_id:
        raise BenchError("run_id 只能含字母、数字、点、下划线和连字符")
    out_dir = Path(args.out) if args.out else results_root / run_id
    work_root = args.work_root / run_id
    if out_dir.exists():
        raise BenchError(f"结果目录 {out_dir} 已存在，拒绝覆盖")
    if work_root.exists():
        raise BenchError(f"工作目录 {work_root} 已存在，拒绝覆盖")
    template = args.executor_cmd or EXECUTOR_TEMPLATES[args.executor]

    out_dir.mkdir(parents=True)
    work_root.mkdir(parents=True)
    if args.executor == "codex" and args.executor_md_sha256 is not None:
        isolated_home = work_root / "_codex_home"
        isolated_home.mkdir()
        (isolated_home / "config.toml").write_text(
            'model_reasoning_effort = "medium"\n', encoding="utf-8")
        shutil.copyfile(args.executor_md_path, isolated_home / "AGENTS.md")
        auth_target = (args.codex_home / "auth.json").resolve(strict=True)
        (isolated_home / "auth.json").symlink_to(auth_target)
    results_path = out_dir / "results.jsonl"
    manifest = {
        "run_id": run_id, "model": args.model, "executor": args.executor,
        "reps": args.reps, "tasks": [meta["id"] for _, meta in tasks],
        "executor_cmd": template, "work_root": str(work_root),
        "executor_md": args.executor_md_record,
        "executor_md_sha256": args.executor_md_sha256,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    count = 0
    for task_dir, meta in tasks:
        for rep in range(args.reps):
            record = run_one(task_dir, meta, bench_dir, args, run_id,
                             work_root, rep, template,
                             args.executor_md_content)
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
            score = record["score"]
            shown = (f"{score['passed']}/{score['total']}" if score
                     else f"评分出错({record['score_error']})")
            print(f"{meta['id']} rep={rep} executor={record['executor_status']}"
                  f" score={shown} honest={record['honest']}"
                  f" wall_s={record['wall_s']}", flush=True)
    print(f"run_id={run_id} 共 {count} 条，结果在 {results_path}")
    return 0


# --------------------------------------------------------------- report

def load_runs(results_root, run_ids):
    records = []
    for run_id in run_ids:
        path = Path(run_id)
        if not path.is_file():
            path = Path(results_root) / run_id / "results.jsonl"
        if not path.is_file():
            raise BenchError(f"找不到 run {run_id} 的结果（{path}）")
        records.extend(read_results(path))
    return records


def median_or_none(values):
    values = [v for v in values if type(v) in (int, float)]
    return round(statistics.median(values), 4) if values else None


def mean_or_none(values):
    values = [v for v in values if type(v) in (int, float)]
    return round(statistics.fmean(values), 4) if values else None


def record_pass_rate(record):
    """一次运行的通过率；评分出错或编译失败按 0 计。"""
    rate = record.get("pass_rate")
    return rate if type(rate) in (int, float) else 0.0


def record_tokens(record):
    t_in, t_out = record.get("tokens_in"), record.get("tokens_out")
    if type(t_in) in (int, float) and type(t_out) in (int, float):
        return t_in + t_out
    return None


def summarize(records):
    """把一组运行记录汇总成一行。"""
    style = [r["style"]["ratio"] for r in records
             if isinstance(r.get("style"), dict)]
    safety = [r.get("safety_ok") for r in records
              if r.get("safety_ok") is not None]
    return {
        "n": len(records),
        "tasks": len({r.get("task") for r in records}),
        "pass_rate": mean_or_none([record_pass_rate(r) for r in records]),
        "full_pass": sum(1 for r in records if record_pass_rate(r) == 1.0),
        "safety_ok": (f"{sum(1 for s in safety if s)}/{len(safety)}"
                      if safety else None),
        "median_wall_s": median_or_none([r.get("wall_s") for r in records]),
        "median_tokens": median_or_none([record_tokens(r) for r in records]),
        "median_cost_usd": median_or_none(
            [r.get("cost_usd") for r in records]),
        "style": mean_or_none(style),
        "tests_written": sum(1 for r in records if r.get("tests_written")),
        "scope_violation_runs": sum(
            1 for r in records if r.get("scope_violations")),
        "report_missing": sum(
            1 for r in records if r.get("report_claim") == "missing"),
        "dishonest": sum(1 for r in records if r.get("honest") is False),
        "executor_failures": sum(
            1 for r in records if r.get("executor_status") != "ok"),
        "leak_hint_runs": sum(1 for r in records if r.get("leak_hints")),
    }


def cmd_report(args):
    results_root = Path(args.results_root
                        or Path(args.bench_dir) / "results")
    records = load_runs(results_root, args.run_ids)
    if args.split:
        records = [r for r in records if r.get("split") == args.split]
    groups = {}
    for record in records:
        key = (record.get("category"), record.get("split"),
               record.get("model"), record.get("executor_md_sha256"))
        groups.setdefault(key, []).append(record)
    rows = []
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        row = {"category": key[0], "split": key[1], "model": key[2],
               "executor_md_sha256": key[3]}
        row.update(summarize(groups[key]))
        rows.append(row)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    columns = ("category", "split", "model", "executor_md_sha256", "n", "tasks", "pass_rate",
               "full_pass", "safety_ok", "median_wall_s", "median_tokens",
               "median_cost_usd", "style", "tests_written",
               "scope_violation_runs", "report_missing", "dishonest",
               "executor_failures", "leak_hint_runs")
    table = [columns] + [
        tuple("-" if row[c] is None else str(row[c]) for c in columns)
        for row in rows]
    widths = [max(len(line[i]) for line in table)
              for i in range(len(columns))]
    for line in table:
        print("  ".join(cell.ljust(widths[i])
                        for i, cell in enumerate(line)).rstrip())
    print(f"样本数合计 {len(records)}；n 为运行次数，pass_rate 为各次通过率的"
          "均值，tests_written / scope_violation_runs / report_missing / "
          "dishonest / executor_failures / leak_hint_runs 为次数")
    return 0


# --------------------------------------------------------------- judge

def load_thresholds(config_path):
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise BenchError(f"读不到配置 {config_path}: {e}")
    except yaml.YAMLError as e:
        raise BenchError(f"配置 {config_path} 不是合法 YAML: {e}")
    thresholds = data.get("thresholds") if isinstance(data, dict) else None
    if not isinstance(thresholds, dict):
        raise BenchError("配置里没有 thresholds")
    result = {}
    for key in ("median_time_ratio", "cost_ratio"):
        value = thresholds.get(key)
        if type(value) not in (int, float) or value <= 0:
            raise BenchError(f"thresholds.{key} 必须是正数")
        result[key] = value
    return result


def is_model(record, name):
    """记录是否属于某个模型：按调用时的名字或实际模型 ID 匹配。

    claude 用别名调用（haiku）时 model 是别名，model_actual 才是真实 ID。
    """
    return name in (record.get("model"), record.get("model_actual"))


def judge_records(records, candidate, incumbent, category, thresholds,
                  executor_md_sha=None):
    """按 DESIGN 6.2 判定，返回结果字典。"""
    in_category = [r for r in records if r.get("category") == category]
    hashes = {r.get("executor_md_sha256") for r in in_category}
    if executor_md_sha is not None:
        selected_sha = (None if executor_md_sha == "none"
                        else executor_md_sha)
        in_category = [r for r in in_category
                       if r.get("executor_md_sha256") == selected_sha]
    elif len(hashes) > 1:
        raise BenchError("judge 结果混合了多个 executor_md_sha256；"
                         "请用 --executor-md-sha 过滤")
    cand_all = [r for r in in_category if is_model(r, candidate)]
    cand = [r for r in cand_all if r.get("split") == "holdout"]
    inc = [r for r in in_category
           if is_model(r, incumbent) and r.get("split") == "holdout"]
    result = {
        "verdict": "reject",
        "category": category,
        "candidate": candidate,
        "incumbent": incumbent,
        "reasons": [],
        "samples": {"candidate": len(cand), "incumbent": len(inc),
                    "candidate_all_splits": len(cand_all)},
        "thresholds": thresholds,
        "metrics": {},
    }
    reasons = result["reasons"]

    # 一票否决：该类别任何一次运行（不分 dev / holdout）谎报测试结果。
    liars = [r for r in cand_all if r.get("honest") is False]
    if liars:
        where = "; ".join(
            f"run {r.get('run_id')} 题 {r.get('task')} 第 {r.get('rep')} 次"
            for r in liars)
        reasons.append(
            f"一票否决：候选模型谎报测试结果（REPORT.md 声称 PASS 但自己的"
            f"测试编译失败或不通过）：{where}")
        result["vetoed"] = True
        return result
    result["vetoed"] = False

    if not cand or not inc:
        reasons.append(
            f"holdout 样本不足，无法比较（候选 {len(cand)} 条，现任 "
            f"{len(inc)} 条）")
        result["decidable"] = False
        return result
    result["decidable"] = True

    cand_rate = mean_or_none([record_pass_rate(r) for r in cand])
    inc_rate = mean_or_none([record_pass_rate(r) for r in inc])
    cand_time = median_or_none([r.get("wall_s") for r in cand])
    inc_time = median_or_none([r.get("wall_s") for r in inc])
    cand_cost = median_or_none([r.get("cost_usd") for r in cand])
    inc_cost = median_or_none([r.get("cost_usd") for r in inc])
    result["metrics"] = {
        "candidate_pass_rate": cand_rate, "incumbent_pass_rate": inc_rate,
        "candidate_median_wall_s": cand_time,
        "incumbent_median_wall_s": inc_time,
        "candidate_median_cost_usd": cand_cost,
        "incumbent_median_cost_usd": inc_cost,
        "candidate_executor_failures": sum(
            1 for r in cand if r.get("executor_status") != "ok"),
        "incumbent_executor_failures": sum(
            1 for r in inc if r.get("executor_status") != "ok"),
    }

    quality_ok = cand_rate >= inc_rate
    reasons.append(
        f"条件 1 通过率：候选 {cand_rate} {'≥' if quality_ok else '<'} "
        f"现任 {inc_rate}")
    if category == "C":
        unsafe = [r for r in cand if r.get("safety_ok") is not True]
        result["metrics"]["candidate_safety_failures"] = len(unsafe)
        if unsafe:
            quality_ok = False
            where = "; ".join(
                f"run {r.get('run_id')} 题 {r.get('task')} 第 "
                f"{r.get('rep')} 次" for r in unsafe)
            reasons.append(
                f"条件 1 安全用例：C 类要求 safety / concurrency / asan / "
                f"tsan 组每次都 100% 通过，未满足：{where}")
        else:
            reasons.append("条件 1 安全用例：全部运行 100% 通过")

    time_ok = (cand_time is not None and inc_time is not None
               and cand_time <= thresholds["median_time_ratio"] * inc_time)
    cost_known = cand_cost is not None and inc_cost is not None
    cost_ok = cost_known and cand_cost <= thresholds["cost_ratio"] * inc_cost
    reasons.append(
        f"条件 2 耗时：候选中位 {cand_time} s，现任中位 {inc_time} s，"
        f"门槛 {thresholds['median_time_ratio']} 倍，"
        f"{'满足' if time_ok else '不满足'}")
    reasons.append(
        "条件 2 花费：" + (
            f"候选中位 {cand_cost}，现任中位 {inc_cost}，门槛 "
            f"{thresholds['cost_ratio']} 倍，{'满足' if cost_ok else '不满足'}"
            if cost_known else "有一方没有花费数据，不参与判定"))

    if quality_ok and (time_ok or cost_ok):
        result["verdict"] = "enter_front"
    elif quality_ok:
        result["verdict"] = "enter_backup"
    else:
        result["verdict"] = "reject"
    return result


def cmd_judge(args):
    results_root = Path(args.results_root
                        or Path(args.bench_dir) / "results")
    records = load_runs(results_root, args.run_ids)
    thresholds = load_thresholds(args.config)
    result = judge_records(records, args.candidate, args.incumbent,
                           args.category, thresholds, args.executor_md_sha)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------- 入口

def build_parser():
    parser = argparse.ArgumentParser(description="执行器模型准入基准")
    parser.add_argument("--bench-dir", default=str(DEFAULT_BENCH),
                        help="bench 目录（默认仓库内 bench/）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("selfcheck", help="检查题目自身是否合格")
    p.add_argument("--tasks", nargs="*", help="只检查这些题（默认全部）")
    p.add_argument("--repeat", type=int, default=1,
                   help="reference 重复评分次数，用来发现偶发失败")
    p.add_argument("--jobs", type=int, default=3, help="并行题数")
    p.add_argument("--tmp", help="临时目录（默认系统临时目录）")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="失败时打印评分输出末尾")
    p.set_defaults(func=cmd_selfcheck)

    p = sub.add_parser("run", help="让执行器模型做题并评分")
    p.add_argument("--model", required=True, help="模型 ID")
    p.add_argument("--executor", required=True, choices=EXECUTORS)
    p.add_argument("--tasks", nargs="*", help="只跑这些题（默认全部）")
    p.add_argument("--split", choices=SPLITS, help="只跑这个划分")
    p.add_argument("--category", choices=CATEGORIES, help="只跑这个类别")
    p.add_argument("--reps", type=int, default=3, help="每题重复次数")
    p.add_argument("--run-id", help="本次运行的名字（默认 时间-模型）")
    p.add_argument("--out", help="结果目录（默认 bench/results/<run_id>）")
    p.add_argument("--results-root",
                   help="结果根目录（默认 bench/results），也用于查重 holdout")
    p.add_argument("--work-root", default="/tmp/agent-router-work",
                   help="工作区根目录（默认 /tmp/agent-router-work）")
    p.add_argument("--codex-home", default="~/agent-router-work/codex_home",
                   help="codex 隔离配置目录（默认 ~/agent-router-work/codex_home）")
    p.add_argument("--confirm-holdout", action="store_true",
                   help="确认运行 holdout 题")
    p.add_argument("--executor-cmd",
                   help="覆盖执行器命令模板（占位符见 bench/README.md）")
    p.add_argument("--executor-timeout", type=int, default=3600,
                   help="单次执行器调用的超时秒数")
    p.add_argument("--executor-md", default=str(DEFAULT_EXECUTOR_MD),
                   help="执行器说明文件路径，或 none 禁用")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="汇总一次或多次运行")
    p.add_argument("run_ids", nargs="+", help="run_id 或 results.jsonl 路径")
    p.add_argument("--results-root", help="结果根目录（默认 bench/results）")
    p.add_argument("--split", choices=SPLITS, help="只看这个划分")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("judge", help="按 DESIGN 6.2 判定候选模型")
    p.add_argument("--candidate", required=True)
    p.add_argument("--incumbent", required=True)
    p.add_argument("--category", required=True, choices=CATEGORIES)
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="读取 thresholds 的配置文件")
    p.add_argument("--results-root", help="结果根目录（默认 bench/results）")
    p.add_argument("--executor-md-sha", help="只比较此执行器说明版本")
    p.add_argument("run_ids", nargs="+", help="run_id 或 results.jsonl 路径")
    p.set_defaults(func=cmd_judge)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
