// mini_test.h：准入基准题的公共断言框架（单头文件，无第三方依赖）。
//
// 用法：
//   #include "mini_test.h"
//   MT_TEST(basic, parsesEmptyInput) {
//       MT_CHECK(foo());
//       MT_CHECK_EQ(bar(), 3);
//       MT_REQUIRE(ptr != nullptr);  // 失败则立即结束本用例
//   }
//   MT_MAIN()  // 只在一个翻译单元里写；评分脚本会自动生成
//
// 计数单位是用例（MT_TEST）：用例内任一断言失败、抛出异常即判该用例失败。
// 每个用例属于一个分组，汇总时按分组输出 [通过数, 总数]。
//
// 可执行文件的命令行：
//   <bin> --list               每行输出 "<group>\t<name>"
//   <bin> --run <group> <name> 只跑一个用例，通过退出码 0，失败 1，找不到 2
//   <bin>                      进程内顺序跑全部用例，最后一行输出
//                              MT_RESULT {"passed":..,"total":..,"groups":{..}}
// 评分脚本用 --list / --run 逐用例起进程，崩溃和死锁只影响单个用例。
#ifndef BENCH_COMMON_MINI_TEST_H_
#define BENCH_COMMON_MINI_TEST_H_

#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <exception>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace mt {

struct Case {
    const char* group;
    const char* name;
    void (*fn)();
};

inline std::vector<Case>& registry() {
    static std::vector<Case> cases;
    return cases;
}

inline std::atomic<int>& failCount() {
    static std::atomic<int> count{0};
    return count;
}

inline std::mutex& printMutex() {
    static std::mutex mutex;
    return mutex;
}

struct Registrar {
    Registrar(const char* group, const char* name, void (*fn)()) {
        registry().push_back(Case{group, name, fn});
    }
};

// MT_REQUIRE 失败时抛出，用于提前结束当前用例。
struct AbortCase {};

template <typename T, typename = void>
struct IsStreamable : std::false_type {};
template <typename T>
struct IsStreamable<
    T, std::void_t<decltype(std::declval<std::ostream&>()
                            << std::declval<const T&>())>>
    : std::true_type {};

template <typename T>
std::string toText(const T& value) {
    std::ostringstream out;
    if constexpr (std::is_same_v<T, bool>) {
        out << (value ? "true" : "false");
    } else if constexpr (std::is_enum_v<T>) {
        out << static_cast<long long>(value);
    } else if constexpr (IsStreamable<T>::value) {
        out.precision(17);
        out << value;
    } else {
        out << "<?>";
    }
    return out.str();
}

inline void reportFail(const char* file, int line, const std::string& text) {
    failCount().fetch_add(1);
    std::lock_guard<std::mutex> lock(printMutex());
    std::fprintf(stdout, "    %s:%d: %s\n", file, line, text.c_str());
    std::fflush(stdout);
}

template <typename A, typename B>
bool checkEq(const char* file, int line, const char* a_text,
             const char* b_text, const A& a, const B& b) {
    if (a == b) {
        return true;
    }
    reportFail(file, line,
               std::string("MT_CHECK_EQ(") + a_text + ", " + b_text +
                   ") 左=" + toText(a) + " 右=" + toText(b));
    return false;
}

inline bool checkNear(const char* file, int line, const char* a_text,
                      const char* b_text, double a, double b, double eps) {
    if (std::isfinite(a) && std::isfinite(b) && std::fabs(a - b) <= eps) {
        return true;
    }
    reportFail(file, line,
               std::string("MT_CHECK_NEAR(") + a_text + ", " + b_text +
                   ") 左=" + toText(a) + " 右=" + toText(b) +
                   " 容差=" + toText(eps));
    return false;
}

// 运行单个用例，返回是否通过。
inline bool runCase(const Case& test_case) {
    const int before = failCount().load();
    try {
        test_case.fn();
    } catch (const AbortCase&) {
        // 已在 MT_REQUIRE 处记录。
    } catch (const std::exception& error) {
        reportFail(test_case.name, 0,
                   std::string("未捕获异常: ") + error.what());
    } catch (...) {
        reportFail(test_case.name, 0, "未捕获的未知异常");
    }
    return failCount().load() == before;
}

inline int runMain(int argc, char** argv) {
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    if (argc >= 2 && std::strcmp(argv[1], "--list") == 0) {
        for (const Case& test_case : registry()) {
            std::printf("%s\t%s\n", test_case.group, test_case.name);
        }
        return 0;
    }
    if (argc >= 4 && std::strcmp(argv[1], "--run") == 0) {
        for (const Case& test_case : registry()) {
            if (std::strcmp(test_case.group, argv[2]) == 0 &&
                std::strcmp(test_case.name, argv[3]) == 0) {
                const bool ok = runCase(test_case);
                std::printf("[%s] %s.%s\n", ok ? "PASS" : "FAIL",
                            test_case.group, test_case.name);
                std::fflush(stdout);
                return ok ? 0 : 1;
            }
        }
        std::printf("未找到用例 %s.%s\n", argv[2], argv[3]);
        return 2;
    }
    // 进程内跑全部用例，保持注册顺序输出分组。
    std::vector<std::string> order;
    std::map<std::string, std::pair<int, int>> stats;
    int passed = 0;
    int total = 0;
    for (const Case& test_case : registry()) {
        if (stats.find(test_case.group) == stats.end()) {
            order.push_back(test_case.group);
        }
        const bool ok = runCase(test_case);
        std::printf("[%s] %s.%s\n", ok ? "PASS" : "FAIL", test_case.group,
                    test_case.name);
        stats[test_case.group].first += ok ? 1 : 0;
        stats[test_case.group].second += 1;
        passed += ok ? 1 : 0;
        total += 1;
    }
    std::printf("MT_RESULT {\"passed\": %d, \"total\": %d, \"groups\": {",
                passed, total);
    for (std::size_t i = 0; i < order.size(); ++i) {
        std::printf("%s\"%s\": [%d, %d]", i == 0 ? "" : ", ",
                    order[i].c_str(), stats[order[i]].first,
                    stats[order[i]].second);
    }
    std::printf("}}\n");
    return passed == total ? 0 : 1;
}

}  // namespace mt

#define MT_TEST(group, name)                                              \
    static void mt_case_##group##_##name();                               \
    static ::mt::Registrar mt_registrar_##group##_##name(                 \
        #group, #name, &mt_case_##group##_##name);                        \
    static void mt_case_##group##_##name()

#define MT_CHECK(cond)                                                    \
    do {                                                                  \
        if (!(cond)) {                                                    \
            ::mt::reportFail(__FILE__, __LINE__,                          \
                             std::string("MT_CHECK(") + #cond + ")");     \
        }                                                                 \
    } while (false)

#define MT_REQUIRE(cond)                                                  \
    do {                                                                  \
        if (!(cond)) {                                                    \
            ::mt::reportFail(__FILE__, __LINE__,                          \
                             std::string("MT_REQUIRE(") + #cond + ")");   \
            throw ::mt::AbortCase{};                                      \
        }                                                                 \
    } while (false)

#define MT_CHECK_EQ(a, b)                                                 \
    (void)::mt::checkEq(__FILE__, __LINE__, #a, #b, (a), (b))

#define MT_REQUIRE_EQ(a, b)                                               \
    do {                                                                  \
        if (!::mt::checkEq(__FILE__, __LINE__, #a, #b, (a), (b))) {       \
            throw ::mt::AbortCase{};                                      \
        }                                                                 \
    } while (false)

#define MT_CHECK_NEAR(a, b, eps)                                          \
    (void)::mt::checkNear(__FILE__, __LINE__, #a, #b,                     \
                          static_cast<double>(a), static_cast<double>(b), \
                          static_cast<double>(eps))

#define MT_CHECK_THROWS(statement, exception_type)                        \
    do {                                                                  \
        bool mt_thrown = false;                                           \
        try {                                                             \
            statement;                                                    \
        } catch (const exception_type&) {                                 \
            mt_thrown = true;                                             \
        } catch (...) {                                                   \
        }                                                                 \
        if (!mt_thrown) {                                                 \
            ::mt::reportFail(__FILE__, __LINE__,                          \
                             std::string("MT_CHECK_THROWS(") +            \
                                 #statement + ", " + #exception_type +    \
                                 ")");                                    \
        }                                                                 \
    } while (false)

#define MT_CHECK_NO_THROW(statement)                                      \
    do {                                                                  \
        try {                                                             \
            statement;                                                    \
        } catch (...) {                                                   \
            ::mt::reportFail(__FILE__, __LINE__,                          \
                             std::string("MT_CHECK_NO_THROW(") +          \
                                 #statement + ")");                       \
        }                                                                 \
    } while (false)

#define MT_MAIN()                                                         \
    int main(int argc, char** argv) { return ::mt::runMain(argc, argv); }

#endif  // BENCH_COMMON_MINI_TEST_H_
