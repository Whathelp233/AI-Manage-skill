"""tools/style_check.py 的单测。"""
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout

TESTS_DIR = Path(__file__).resolve().parent
MODULE_DIR = TESTS_DIR.parent
if not (MODULE_DIR / "style_check.py").is_file():
    MODULE_DIR = MODULE_DIR / "tools"
sys.path.insert(0, str(MODULE_DIR))
import style_check  # noqa: E402

CLEAN_HEADER = """\
// 泵控制器。
#ifndef DEMO_PUMP_DEMO_PUMP_CONTROLLER_H_
#define DEMO_PUMP_DEMO_PUMP_CONTROLLER_H_

#include <cstdint>
#include <functional>
#include <mutex>
#include <string>

namespace demo {

constexpr double MAX_SPEED_MPS = 1.5;
#define DEMO_STRINGIFY(x) #x

enum class PumpState { OFF, WARMING_UP, ON };

enum class Level : std::uint8_t {
    LOW = 0,
    HIGH = 1 << 2,
};

struct PumpConfig {
    std::int64_t clear_delay_ms = 3000;
};

class Base;

class PumpController final : public Base {
public:
    using Sink = std::function<void(bool pump_on)>;

    explicit PumpController(const PumpConfig& config);
    ~PumpController();
    PumpController(const PumpController&) = delete;
    PumpController& operator=(const PumpController&) = delete;

    bool start(std::int64_t run_ms, std::int64_t now_ms);
    void setPersonPresent(bool present,
                          std::int64_t now_ms);
    const PumpConfig& config() const;
    static bool isValid(const PumpConfig& config);
    bool operator==(const PumpController& other) const;
    explicit operator bool() const;
    std::int64_t remainingMs() const { return remaining_ms_; }

    inline int helper() {
        std::lock_guard<std::mutex> state_lock(state_mutex_);
        return 1;
    }

private:
    PumpConfig config_;
    std::mutex state_mutex_;
    std::function<void(bool)> pump_sink_;
    std::int64_t remaining_ms_ = 0;
};

inline std::string describeState(PumpState state) {
    std::string state_name(state == PumpState::ON ? "on" : "off");
    return state_name;
}

}  // namespace demo

#endif  // DEMO_PUMP_DEMO_PUMP_CONTROLLER_H_
"""

CLEAN_SOURCE = """\
#include "demo_pump_controller.h"

#include <ctime>
#include <memory>
#include <vector>

namespace demo {

namespace {

const std::string DEFAULT_NAME("pump");
std::mutex g_registry_mutex;

bool isBlank(char ch) {
    return ch == ' ' || ch == '\\t';  // 字符串里的 "\\t" 不算 Tab
}

std::vector<std::pair<int, int>> cellsInRange(int first, int last) {
    std::vector<std::pair<int, int>> cells;
    for (int cell_index = first; cell_index < last; ++cell_index) {
        cells.emplace_back(cell_index, cell_index);
    }
    return cells;
}

}  // namespace

PumpController::PumpController(const PumpConfig& config) : config_(config) {}

PumpController::~PumpController() = default;

bool PumpController::start(std::int64_t run_ms, std::int64_t now_ms) {
    std::unique_lock<std::mutex> state_lock(state_mutex_);
    struct tm* local_time = nullptr;
    const auto deleter = [](int* raw_value) { delete raw_value; };
    std::unique_ptr<int, decltype(deleter)> owned_value(new int(1), deleter);
    const char* text = "camelCase_ in a string, int badName = 1;";
    static_cast<void>(local_time);
    static_cast<void>(text);
    return run_ms > 0 && now_ms >= 0 && isBlank(' ');
}

}  // namespace demo
"""


class StyleCheckTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="style_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def put(self, name, text, raw=False):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw:
            path.write_bytes(text)
        else:
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def check(self):
        return style_check.run_checks([str(self.tmp)])

    def failed(self):
        result = self.check()
        return sorted(name for name, item in result["checks"].items()
                      if not item["ok"])

    def put_clean(self):
        self.put("include/demo_pump/demo_pump_controller.h", CLEAN_HEADER)
        self.put("src/demo_pump_controller.cpp", CLEAN_SOURCE)


class CleanCodeTest(StyleCheckTestCase):
    def test_clean_code_passes_everything(self):
        self.put_clean()
        result = self.check()
        examples = {name: item["examples"]
                    for name, item in result["checks"].items()
                    if not item["ok"]}
        self.assertEqual(examples, {})
        self.assertEqual(result["passed"], result["total"])
        self.assertEqual(result["total"], len(style_check.CHECK_NAMES))
        self.assertEqual(result["ratio"], 1.0)
        self.assertEqual(result["files"], 2)

    def test_empty_directory(self):
        result = self.check()
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["passed"], result["total"])

    def test_non_cpp_files_are_ignored(self):
        self.put("Makefile", "all:\n\techo tab is fine here   \n")
        self.put("notes.md", "x" * 200 + "\n")
        self.assertEqual(self.check()["files"], 0)

    def test_pragma_once_is_a_valid_guard(self):
        self.put("a.h", "#pragma once\n\nnamespace demo {}\n")
        self.assertEqual(self.failed(), [])


class FormatChecksTest(StyleCheckTestCase):
    def test_line_width(self):
        self.put_clean()
        self.put("src/wide.cpp", "// " + "x" * 78 + "\n")
        self.assertEqual(self.failed(), ["line_width"])
        result = self.check()
        self.assertEqual(result["checks"]["line_width"]["violations"], 1)
        self.assertIn("wide.cpp:1", result["checks"]["line_width"]
                      ["examples"][0])

    def test_line_width_counts_characters_not_bytes(self):
        # 80 个汉字是 240 字节，但只有 80 列。
        self.put("src/wide.cpp", "// " + "泵" * 77 + "\n")
        self.assertEqual(self.failed(), [])
        self.put("src/wide.cpp", "// " + "泵" * 78 + "\n")
        self.assertEqual(self.failed(), ["line_width"])

    def test_tabs(self):
        self.put("src/tab.cpp", "int foo() {\n\treturn 1;\n}\n")
        self.assertEqual(self.failed(), ["no_tabs"])

    def test_trailing_whitespace(self):
        self.put("src/space.cpp", "int foo() {   \n    return 1;\n}\n")
        self.assertEqual(self.failed(), ["no_trailing_whitespace"])

    def test_crlf_is_not_trailing_whitespace(self):
        self.put("src/crlf.cpp", b"int foo() {\r\n    return 1;\r\n}\r\n",
                 raw=True)
        self.assertEqual(self.failed(), [])

    def test_bom(self):
        self.put("src/bom.cpp", b"\xef\xbb\xbfint foo();\n", raw=True)
        self.assertEqual(self.failed(), ["utf8_no_bom"])

    def test_invalid_utf8(self):
        self.put("src/latin.cpp", b"// caf\xe9\nint foo();\n", raw=True)
        self.assertEqual(self.failed(), ["utf8_no_bom"])

    def test_file_names(self):
        self.put("src/PumpController.cpp", "int foo();\n")
        self.put("src/pump-controller.cpp", "int foo();\n")
        self.put("src/pump.cc", "int foo();\n")
        self.put("include/pump.hpp", "#pragma once\n")
        result = self.check()
        self.assertEqual(self.failed(), ["file_names"])
        self.assertEqual(result["checks"]["file_names"]["violations"], 4)

    def test_missing_header_guard(self):
        self.put("include/a.h", "namespace demo {}\n")
        self.assertEqual(self.failed(), ["header_guards"])

    def test_mismatched_header_guard(self):
        self.put("include/a.h", "#ifndef A_H_\n#define B_H_\n#endif\n")
        self.assertEqual(self.failed(), ["header_guards"])

    def test_using_namespace_in_header(self):
        self.put("include/a.h", "#pragma once\nusing namespace std;\n")
        self.put("src/a.cpp", "using namespace std;\n")
        result = self.check()
        self.assertEqual(self.failed(), ["no_using_namespace_in_headers"])
        self.assertEqual(
            result["checks"]["no_using_namespace_in_headers"]["violations"],
            1)

    def test_commented_using_namespace_is_fine(self):
        self.put("include/a.h",
                 "#pragma once\n// using namespace std; 不要这样写\n")
        self.assertEqual(self.failed(), [])


class NamingChecksTest(StyleCheckTestCase):
    def test_type_names(self):
        self.put("include/a.h", """\
            #pragma once
            class pump_controller {
            };
            struct navState {
            };
            enum class pump_state {
                OFF,
            };
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["type_names"])
        self.assertEqual(result["checks"]["type_names"]["violations"], 3)

    def test_function_names_in_header(self):
        self.put("include/a.h", """\
            #pragma once
            class PumpController {
            public:
                bool request_start(int now_ms);
                void RequestStop();
                static int Count();
                std::size_t dropped_count() const;
            };
            double compute_dose(double time_s);
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["function_names"])
        self.assertEqual(result["checks"]["function_names"]["violations"], 5)

    def test_function_names_in_source(self):
        self.put("src/a.cpp", """\
            bool PumpController::request_start(int now_ms) {
                return now_ms > 0;
            }
            static bool parse_number(const char* text) {
                return text != nullptr;
            }
            void PumpController::Stop() {}
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["function_names"])
        self.assertEqual(result["checks"]["function_names"]["violations"], 3)

    def test_variable_names(self):
        self.put("src/a.cpp", """\
            int foo(int sensorId, const std::string& mapPath) {
                double maxSpeed = 1.0;
                bool isReady;
                auto fooBar = 3;
                return sensorId;
            }
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["variable_names"])
        self.assertEqual(result["checks"]["variable_names"]["violations"], 5)

    def test_member_names(self):
        self.put("include/a.h", """\
            #pragma once
            class PumpController {
            private:
                int batteryLevel_;
                int m_count;
                int Speed_;
            };
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["variable_names"])
        # batteryLevel_ 同时被“camelCase 变量”和“成员后缀”两条规则报告。
        self.assertGreaterEqual(
            result["checks"]["variable_names"]["violations"], 3)

    def test_constant_names(self):
        self.put("include/a.h", """\
            #pragma once
            constexpr double kMaxSpeed = 1.0;
            static constexpr int max_count = 3;
            #define square(x) ((x) * (x))
            enum class State { Off, ON, warming_up };
            enum Legacy {
                LEGACY_A,
                legacyB = 2,
            };
            """)
        result = self.check()
        self.assertEqual(self.failed(), ["constant_names"])
        self.assertEqual(result["checks"]["constant_names"]["violations"], 6)

    def test_constexpr_function_is_not_a_constant(self):
        self.put("include/a.h", """\
            #pragma once
            constexpr int squareOf(int value) {
                return value * value;
            }
            """)
        self.assertEqual(self.failed(), [])

    def test_comments_and_strings_are_ignored(self):
        self.put("src/a.cpp", """\
            // class bad_name {  int fooBar = 1;  #define lower 1
            /* struct another_bad {
               bool PumpController::bad_method() */
            const char* text = "class bad_name { int fooBar; }";
            const char* raw = R"(enum class E { lower_value };)";
            """)
        self.assertEqual(self.failed(), [])


class CommandLineTest(StyleCheckTestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = style_check.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output(self):
        self.put_clean()
        self.put("src/tab.cpp", "int foo() {\n\treturn 1;\n}\n")
        code, out, _ = self.run_main(str(self.tmp))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data),
                         {"passed", "total", "ratio", "files", "checks"})
        self.assertEqual(data["total"], 11)
        self.assertEqual(data["passed"], 10)
        self.assertEqual(data["ratio"], round(10 / 11, 4))
        self.assertFalse(data["checks"]["no_tabs"]["ok"])

    def test_single_file_and_max_examples(self):
        path = self.put("src/tab.cpp", b"\tint a;\n\tint b;\n\tint c;\n",
                        raw=True)
        code, out, _ = self.run_main(str(path), "--max-examples", "2")
        data = json.loads(out)
        self.assertEqual(data["files"], 1)
        self.assertEqual(data["checks"]["no_tabs"]["violations"], 3)
        self.assertEqual(len(data["checks"]["no_tabs"]["examples"]), 2)

    def test_missing_path(self):
        code, _, err = self.run_main(str(self.tmp / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("路径不存在", err)


if __name__ == "__main__":
    unittest.main()
