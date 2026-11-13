#!/usr/bin/env python3
"""C++ 规范检查（D 类评分）。

对给定目录或文件里的 .h / .cpp 做一组机械检查，每个检查项只有通过或
不通过两种结果（零违规才算通过），输出通过项比例 JSON。

命名检查用正则做，尽力而为：只报有把握的违规，宁可漏报不误报。识别不了
的情况（没有 `_` 后缀的成员变量、缺单位后缀的变量、非常见类型的局部变量）
不在检查范围内，需要人工 review。

用法：
    python3 tools/style_check.py DIR_OR_FILE [...] [--max-examples N]

只用标准库。
"""
import argparse
import json
import os
import re
import sys

MAX_LINE_WIDTH = 80
SOURCE_SUFFIXES = (".h", ".cpp")
# 不符合规范的后缀也收集进来，由 file_names 检查项报告。
FOREIGN_SUFFIXES = (".hpp", ".hh", ".hxx", ".cc", ".cxx")

CHECK_NAMES = (
    "line_width",
    "no_tabs",
    "no_trailing_whitespace",
    "utf8_no_bom",
    "file_names",
    "header_guards",
    "no_using_namespace_in_headers",
    "type_names",
    "function_names",
    "variable_names",
    "constant_names",
)

UPPER_CAMEL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
LOWER_CAMEL = re.compile(r"^[a-z][A-Za-z0-9]*$")
UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
FILE_NAME = re.compile(r"^[a-z][a-z0-9_]*\.(h|cpp)$")

# class / struct / enum 定义或前置声明：名字后面只能是继承列表、{ 或 ;。
TYPE_DECL = re.compile(
    r"^\s*(?:class|struct|enum(?:\s+class|\s+struct)?)\s+"
    r"([A-Za-z_]\w*)\s*(?:final\s*)?(?::[^:;]*)?[{;]?\s*$")
ENUM_BODY = re.compile(
    r"\benum\b(?:\s+(?:class|struct))?(?:\s+[A-Za-z_]\w*)?"
    r"(?:\s*:\s*[\w:\s]+?)?\s*\{([^{}]*)\}")
MACRO_DEF = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")
CONSTEXPR_VAR = re.compile(
    r"^\s*(?:(?:static|inline)\s+)*constexpr\s+"
    r"(?:(?:static|inline|const|unsigned|signed|long|short)\s+)*"
    r"[\w:]+(?:<[^;=(){}]*>)?\s+([A-Za-z_]\w*)\s*(?:=|\{)")
# 类外的成员函数定义：Class::method(
MEMBER_DEF = re.compile(r"\b([A-Z]\w*)::(~?[A-Za-z_]\w*)\s*\(")
# 带返回类型的函数声明或定义（只在低缩进的行上使用）。
FUNC_DECL = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:(?:virtual|static|inline|constexpr|friend)\s+|\[\[\w+\]\]\s*)*"
    r"(?P<ret>(?:const\s+)?[A-Za-z_][\w:]*(?:<[^;(){}]*>)?"
    r"(?:\s+const)?(?:\s*[&*]+)?)"
    r"(?P<sep>\s+|(?<=[&*]))(?P<name>[A-Za-z_]\w*)\s*\(")
NOT_RETURN_TYPES = {
    "return", "else", "new", "delete", "throw", "case", "typedef", "using",
    "if", "while", "for", "switch", "do", "goto", "namespace", "class",
    "struct", "enum", "template", "typename", "operator", "public",
    "private", "protected", "co_return", "co_yield", "co_await", "explicit",
    "sizeof", "not", "and", "or",
}
NOT_FUNCTION_NAMES = {"operator", "main", "if", "while", "for", "switch",
                      "return", "sizeof", "catch", "alignas", "decltype"}
# 常见内建 / 标准类型后面的 camelCase 变量名。
BASIC_TYPES = (
    r"(?:bool|char|short|int|long|float|double|auto|unsigned|signed|"
    r"size_t|std::size_t|u?int(?:8|16|32|64)_t|std::u?int(?:8|16|32|64)_t|"
    r"std::string|string)")
CAMEL_VAR = re.compile(
    r"(?<![\w:])" + BASIC_TYPES + r"(?:\s*[&*]+\s*|\s+)"
    r"([a-z][a-z0-9]*[A-Z]\w*)\s*(?=[=;,)\[{])")
MEMBER_SUFFIX = re.compile(r"(?<![\w.])([A-Za-z]\w*_)(?!\w)")
SNAKE_MEMBER = re.compile(r"^[a-z][a-z0-9_]*_$")
HUNGARIAN_MEMBER = re.compile(r"(?<![\w:])(m_[A-Za-z]\w*)\b")


def strip_code(text):
    """去掉注释和字符串内容，保持行数和列位置不变。"""
    out = []
    i = 0
    n = len(text)
    state = "code"
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                out.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                out.append("  ")
                i += 2
                continue
            if ch == '"':
                # 原始字符串 R"delim( ... )delim"
                raw = re.search(r'R$', text[max(0, i - 1):i])
                if raw:
                    m = re.match(r'"([^()\\\s]{0,16})\(', text[i:])
                    if m:
                        end_mark = ")" + m.group(1) + '"'
                        end = text.find(end_mark, i + m.end())
                        end = n if end < 0 else end + len(end_mark)
                        for c in text[i:end]:
                            out.append("\n" if c == "\n" else " ")
                        i = end
                        continue
                state = "string"
                out.append('"')
                i += 1
                continue
            if ch == "'":
                # 数字分隔符 1'000 不算字符字面量。
                if i > 0 and text[i - 1].isalnum() and nxt.isalnum():
                    out.append(ch)
                    i += 1
                    continue
                state = "char"
                out.append("'")
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
                out.append("\n")
            else:
                out.append(" ")
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                out.append("  ")
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
        else:  # string / char
            quote = '"' if state == "string" else "'"
            if ch == "\\" and i + 1 < n:
                out.append("  " if nxt != "\n" else " \n")
                i += 2
            elif ch == quote:
                state = "code"
                out.append(quote)
                i += 1
            elif ch == "\n":
                state = "code"
                out.append("\n")
                i += 1
            else:
                out.append(" ")
                i += 1
    return "".join(out)


def line_scopes(code_lines):
    """返回每行行首所在的作用域：outer（文件、命名空间、类）或 inner。

    inner 包括函数体、枚举、花括号初始化等，里面不会出现函数声明。
    """
    stack = []
    head = []
    result = []
    for line in code_lines:
        result.append("inner" if "inner" in stack else "outer")
        if line.lstrip().startswith("#"):
            continue
        for ch in line:
            if ch == "{":
                text = "".join(head)
                if re.search(r"\benum\b", text):
                    kind = "inner"
                elif (re.search(r"\b(namespace|extern|class|struct|union)\b",
                                text)
                      and "(" not in text and "=" not in text):
                    kind = "outer"
                else:
                    kind = "inner"
                stack.append(kind)
                head = []
            elif ch == "}":
                if stack:
                    stack.pop()
                head = []
            elif ch == ";":
                head = []
            else:
                head.append(ch)
        head.append(" ")
    return result


def collect_files(paths):
    files = []
    for path in paths:
        if os.path.isfile(path):
            if path.endswith(SOURCE_SUFFIXES + FOREIGN_SUFFIXES):
                files.append(path)
            continue
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                if name.endswith(SOURCE_SUFFIXES + FOREIGN_SUFFIXES):
                    files.append(os.path.join(root, name))
    return files


class Report:
    def __init__(self, max_examples):
        self._max_examples = max_examples
        self._violations = {name: 0 for name in CHECK_NAMES}
        self._examples = {name: [] for name in CHECK_NAMES}

    def add(self, check, path, line_no, detail):
        self._violations[check] += 1
        if len(self._examples[check]) < self._max_examples:
            where = f"{path}:{line_no}" if line_no else path
            self._examples[check].append(f"{where}: {detail}")

    def to_dict(self, file_count):
        checks = {}
        passed = 0
        for name in CHECK_NAMES:
            ok = self._violations[name] == 0
            passed += 1 if ok else 0
            checks[name] = {
                "ok": ok,
                "violations": self._violations[name],
                "examples": self._examples[name],
            }
        total = len(CHECK_NAMES)
        return {
            "passed": passed,
            "total": total,
            "ratio": round(passed / total, 4),
            "files": file_count,
            "checks": checks,
        }


def check_function_name(name, class_name=None):
    """函数名合规返回 True。"""
    if name in NOT_FUNCTION_NAMES or name.startswith("~"):
        return True
    if class_name is not None and name == class_name:
        return True  # 构造函数
    if UPPER_SNAKE.match(name) and ("_" in name or len(name) > 1):
        return True  # 宏或常量，不是函数
    return bool(LOWER_CAMEL.match(name))


def check_naming(path, code_lines, report):
    code = "\n".join(code_lines)
    scopes = line_scopes(code_lines)

    for line_no, line in enumerate(code_lines, 1):
        if not line.strip():
            continue
        is_preprocessor = line.lstrip().startswith("#")

        m = MACRO_DEF.match(line)
        if m and not UPPER_SNAKE.match(m.group(1)):
            report.add("constant_names", path, line_no,
                       f"宏 {m.group(1)} 应为全大写蛇形")
        if is_preprocessor:
            continue

        m = TYPE_DECL.match(line)
        if m and not UPPER_CAMEL.match(m.group(1)):
            report.add("type_names", path, line_no,
                       f"类型 {m.group(1)} 应为大写驼峰")

        m = CONSTEXPR_VAR.match(line)
        if m and not UPPER_SNAKE.match(m.group(1)):
            report.add("constant_names", path, line_no,
                       f"常量 {m.group(1)} 应为全大写蛇形")

        for m in MEMBER_DEF.finditer(line):
            class_name, name = m.group(1), m.group(2)
            if not check_function_name(name, class_name):
                report.add("function_names", path, line_no,
                           f"方法 {class_name}::{name} 应为小写驼峰")

        m = FUNC_DECL.match(line)
        if m and scopes[line_no - 1] == "outer":
            ret_first = re.match(r"(?:const\s+)?([A-Za-z_]\w*)",
                                 m.group("ret")).group(1)
            name = m.group("name")
            # 只看文件、命名空间、类作用域里的声明和定义，避开函数体内带
            # 构造参数的局部变量；g_ 前缀的是全局变量。
            if (ret_first not in NOT_RETURN_TYPES
                    and not name.startswith("g_")
                    and not check_function_name(name)):
                report.add("function_names", path, line_no,
                           f"函数 {name} 应为小写驼峰")

        # constexpr 常量已由 constant_names 检查，不在这里重复报告。
        if not re.search(r"\bconstexpr\b", line):
            for m in CAMEL_VAR.finditer(line):
                report.add("variable_names", path, line_no,
                           f"变量 {m.group(1)} 应为小写蛇形")
        for m in MEMBER_SUFFIX.finditer(line):
            name = m.group(1)
            if name.startswith("__") or UPPER_SNAKE.match(name):
                continue  # 头文件保护宏、保留标识符
            if not SNAKE_MEMBER.match(name):
                report.add("variable_names", path, line_no,
                           f"成员 {name} 应为小写蛇形加 _ 后缀")
        for m in HUNGARIAN_MEMBER.finditer(line):
            report.add("variable_names", path, line_no,
                       f"成员 {m.group(1)} 应为小写蛇形加 _ 后缀")

    for m in ENUM_BODY.finditer(code):
        line_no = code.count("\n", 0, m.start(1)) + 1
        for item in m.group(1).split(","):
            name = item.split("=")[0].strip()
            if name and not UPPER_SNAKE.match(name):
                report.add("constant_names", path, line_no,
                           f"枚举值 {name} 应为全大写蛇形")


def check_file(path, report):
    name = os.path.basename(path)
    if not FILE_NAME.match(name):
        report.add("file_names", path, 0,
                   "文件名应为小写蛇形，后缀 .h 或 .cpp")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as error:
        report.add("utf8_no_bom", path, 0, f"无法读取: {error}")
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        report.add("utf8_no_bom", path, 1, "文件带 BOM")
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        report.add("utf8_no_bom", path, 0, "不是合法的 UTF-8")
        text = raw.decode("utf-8", errors="replace")

    lines = text.split("\n")
    for line_no, line in enumerate(lines, 1):
        line = line.rstrip("\r")
        if len(line) > MAX_LINE_WIDTH:
            report.add("line_width", path, line_no,
                       f"{len(line)} 列，超过 {MAX_LINE_WIDTH}")
        if "\t" in line:
            report.add("no_tabs", path, line_no, "含 Tab")
        if line != line.rstrip():
            report.add("no_trailing_whitespace", path, line_no, "行尾空白")

    code_lines = strip_code(text).split("\n")
    is_header = not path.endswith((".cpp", ".cc", ".cxx"))
    if is_header:
        joined = "\n".join(code_lines)
        has_pragma = re.search(r"^\s*#\s*pragma\s+once\b", joined, re.M)
        guard = re.search(
            r"^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", joined, re.M)
        if not has_pragma and not guard:
            report.add("header_guards", path, 0,
                       "缺少 #pragma once 或 #ifndef/#define 头文件保护")
        for line_no, line in enumerate(code_lines, 1):
            if re.search(r"\busing\s+namespace\b", line):
                report.add("no_using_namespace_in_headers", path, line_no,
                           "头文件中不得 using namespace")
    check_naming(path, code_lines, report)


def run_checks(paths, max_examples=5):
    """检查给定路径，返回结果字典。"""
    files = collect_files(paths)
    report = Report(max_examples)
    for path in files:
        check_file(path, report)
    return report.to_dict(len(files))


def main(argv=None):
    parser = argparse.ArgumentParser(description="C++ 规范检查（D 类评分）")
    parser.add_argument("paths", nargs="+", help="要检查的目录或文件")
    parser.add_argument("--max-examples", type=int, default=5,
                        help="每个检查项最多列出的违规示例数")
    args = parser.parse_args(argv)
    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        print(f"error: 路径不存在: {', '.join(missing)}", file=sys.stderr)
        return 2
    result = run_checks(args.paths, max(0, args.max_examples))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
