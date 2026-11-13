#!/usr/bin/env bash
# score_lib.sh：各题 hidden/score.sh 共用的评分流程。
#
# 调用方（在工作区根目录执行的 hidden/score.sh）先设置变量再调用 sl_main：
#   SL_GROUPS          "组名:用例数 ..."，顺序即输出顺序；含 asan / tsan 组
#                      （各 1 项）。必须与隐藏测试里的用例数一致。
#   SL_TESTS           隐藏测试源文件，相对工作区根，空格分隔。
#   SL_SANITIZERS      "" / "asan" / "asan tsan"。
#   SL_CASE_TIMEOUT_S  单用例超时秒数，默认 10；sanitizer 版本乘 3。
#   SL_EXTRA_FLAGS     追加给 g++ 的参数，可空。
#
# 被测代码的约定：头文件在 include/ 下，实现在 src/ 下（递归收集全部
# .cpp），src/ 里不能有 main。
#
# 计分：
#   - 普通构建：每个 MT_TEST 用例一项，按 --run 单独起进程，退出码 0 为通过。
#   - asan 组（1 项）：-fsanitize=address,undefined 构建下跑全部用例，没有
#     任何 sanitizer 报告、没有崩溃或超时才算通过；断言失败不影响这一项。
#   - tsan 组（1 项）：-fsanitize=thread 构建，规则同上，用 setarch -R 运行。
#   - 普通构建编译失败：全部 0 分，compile_ok 为 false。
#
# 诊断模式：环境变量 BENCH_ONLY_TESTS 给出测试源文件时，只编译这些文件、
# 不跑 sanitizer、不核对 SL_GROUPS，分组按实际用例输出。selfcheck 用它
# 验证 B 类的 regression 用例在 starter 上全部通过。
#
# 输出：过程信息在前，stdout 最后一行是 INTERFACE.md 规定的 JSON。
# 退出码：正常评分一律 0；只有评分脚本自身出错（声明的用例数与实际不符、
# TSan 环境不可用等）才非 0。

set -u

SL_BUILD_DIR="hidden/build"
SL_SAN_PATTERN='ThreadSanitizer|AddressSanitizer|LeakSanitizer'
SL_SAN_PATTERN="${SL_SAN_PATTERN}|UndefinedBehaviorSanitizer|runtime error:"
SL_SAN_EXIT_CODE=97

declare -a SL_ORDER=()
declare -A SL_PASS=()
declare -A SL_TOTAL=()
declare -A SL_PLAIN_TIMEOUT=()

sl_die() {
    echo "score_lib 错误: $*" >&2
    exit 2
}

sl_emit() {
    local compile_ok="$1"
    local passed=0 total=0 parts="" group
    for group in "${SL_ORDER[@]}"; do
        passed=$((passed + SL_PASS[$group]))
        total=$((total + SL_TOTAL[$group]))
        parts+="${parts:+, }\"${group}\": [${SL_PASS[$group]}, "
        parts+="${SL_TOTAL[$group]}]"
    done
    printf '{"passed": %d, "total": %d, "groups": {%s}, "compile_ok": %s}\n' \
        "$passed" "$total" "$parts" "$compile_ok"
}

sl_variant_flags() {
    case "$1" in
        plain) echo "" ;;
        asan)
            echo "-fsanitize=address,undefined -fsanitize=float-cast-overflow" \
                "-fno-sanitize-recover=undefined,float-cast-overflow" \
                "-fno-omit-frame-pointer"
            ;;
        tsan) echo "-fsanitize=thread" ;;
        *) sl_die "未知构建变体 $1" ;;
    esac
}

# 后台编译一个变体，结果写 compile_<variant>.rc。
sl_compile() {
    local variant="$1"
    local sources=""
    if [ -d src ]; then
        sources="$(find src -name '*.cpp' | sort | tr '\n' ' ')"
    fi
    # shellcheck disable=SC2046,SC2086
    g++ -std=c++17 -O1 -g -pthread -w $(sl_variant_flags "$variant") \
        ${SL_EXTRA_FLAGS:-} -Iinclude -Ihidden -Ihidden/common \
        $sources $SL_TESTS "$SL_BUILD_DIR/mt_main.cpp" \
        -o "$SL_BUILD_DIR/test_$variant" \
        > "$SL_BUILD_DIR/compile_$variant.log" 2>&1
    echo $? > "$SL_BUILD_DIR/compile_$variant.rc"
}

# 运行一个用例；结果放在 SL_RC，日志路径放在 SL_LOG。
sl_run_case() {
    local variant="$1" group="$2" name="$3"
    local timeout_s="${SL_CASE_TIMEOUT_S:-10}"
    local -a prefix=()
    if [ "$variant" != "plain" ]; then
        timeout_s=$((timeout_s * 3))
    fi
    if [ "$variant" = "tsan" ]; then
        prefix=(setarch "$(uname -m)" -R)
    fi
    SL_LOG="$SL_BUILD_DIR/logs/$variant.$group.$name.log"
    ASAN_OPTIONS="exitcode=$SL_SAN_EXIT_CODE:detect_leaks=1" \
    LSAN_OPTIONS="exitcode=$SL_SAN_EXIT_CODE" \
    UBSAN_OPTIONS="print_stacktrace=1" \
    TSAN_OPTIONS="exitcode=$SL_SAN_EXIT_CODE:second_deadlock_stack=1" \
        timeout -k 2 "$timeout_s" "${prefix[@]}" \
        "$SL_BUILD_DIR/test_$variant" --run "$group" "$name" \
        > "$SL_LOG" 2>&1 < /dev/null
    SL_RC=$?
}

# 在 sanitizer 构建下跑全部用例，干净返回 0。
sl_sanitizer_clean() {
    local variant="$1"
    local clean=0 group name
    while IFS=$'\t' read -r group name; do
        [ -n "$group" ] || continue
        if [ -n "${SL_PLAIN_TIMEOUT[$group.$name]:-}" ]; then
            echo "  [$variant] $group.$name 普通构建已超时，判不干净"
            clean=1
            continue
        fi
        sl_run_case "$variant" "$group" "$name"
        if grep -q "unexpected memory mapping" "$SL_LOG"; then
            sl_die "TSan 无法在本环境运行（setarch -R 不生效）"
        fi
        if [ "$SL_RC" -ge 124 ] || [ "$SL_RC" -eq "$SL_SAN_EXIT_CODE" ] ||
            grep -Eq "$SL_SAN_PATTERN" "$SL_LOG"; then
            echo "  [$variant] $group.$name 不干净 (rc=$SL_RC)"
            grep -E -m 3 "$SL_SAN_PATTERN" "$SL_LOG" | cut -c1-200 |
                sed 's/^/      /'
            clean=1
        fi
    done < "$SL_BUILD_DIR/cases.txt"
    return $clean
}

sl_main() {
    local entry group count variant diagnostic=0
    [ -n "${SL_GROUPS:-}" ] || sl_die "未设置 SL_GROUPS"
    [ -n "${SL_TESTS:-}" ] || sl_die "未设置 SL_TESTS"
    if [ -n "${BENCH_ONLY_TESTS:-}" ]; then
        diagnostic=1
        SL_TESTS="$BENCH_ONLY_TESTS"
        SL_SANITIZERS=""
    fi
    for entry in $SL_GROUPS; do
        group="${entry%%:*}"
        count="${entry##*:}"
        SL_ORDER+=("$group")
        SL_PASS[$group]=0
        SL_TOTAL[$group]=$count
    done

    rm -rf "$SL_BUILD_DIR"
    mkdir -p "$SL_BUILD_DIR/logs"
    printf '#include "mini_test.h"\nMT_MAIN()\n' > "$SL_BUILD_DIR/mt_main.cpp"

    # 各变体并行编译。
    for variant in plain ${SL_SANITIZERS:-}; do
        sl_compile "$variant" &
    done
    wait

    if [ "$(cat "$SL_BUILD_DIR/compile_plain.rc" 2>/dev/null)" != "0" ]; then
        echo "编译失败（普通构建），日志节选："
        head -n 40 "$SL_BUILD_DIR/compile_plain.log" | cut -c1-240
        if [ "$diagnostic" -eq 1 ]; then
            SL_ORDER=()
        fi
        sl_emit false
        exit 0
    fi

    if ! timeout 10 "$SL_BUILD_DIR/test_plain" --list \
        > "$SL_BUILD_DIR/cases.txt" 2> "$SL_BUILD_DIR/list.err"; then
        echo "测试程序无法列出用例（启动即崩溃），全部判失败"
        sl_emit true
        exit 0
    fi

    # 核对声明的用例数。
    declare -A actual=()
    while IFS=$'\t' read -r group name; do
        [ -n "$group" ] || continue
        actual[$group]=$((${actual[$group]:-0} + 1))
    done < "$SL_BUILD_DIR/cases.txt"
    if [ "$diagnostic" -eq 1 ]; then
        SL_ORDER=()
        SL_PASS=()
        SL_TOTAL=()
        while IFS=$'\t' read -r group name; do
            [ -n "$group" ] || continue
            if [ -z "${SL_TOTAL[$group]:-}" ]; then
                SL_ORDER+=("$group")
                SL_PASS[$group]=0
                SL_TOTAL[$group]=${actual[$group]}
            fi
        done < "$SL_BUILD_DIR/cases.txt"
    else
        for group in "${SL_ORDER[@]}"; do
            case "$group" in
                asan | tsan)
                    [ "${SL_TOTAL[$group]}" = "1" ] ||
                        sl_die "$group 组必须声明为 1 项"
                    ;;
                *)
                    [ "${actual[$group]:-0}" = "${SL_TOTAL[$group]}" ] ||
                        sl_die "组 $group 声明 ${SL_TOTAL[$group]} 项，" \
                            "实际 ${actual[$group]:-0} 项"
                    ;;
            esac
        done
        for group in "${!actual[@]}"; do
            [ -n "${SL_TOTAL[$group]:-}" ] ||
                sl_die "隐藏测试里的组 $group 没有在 SL_GROUPS 中声明"
        done
    fi

    # 普通构建逐用例计分。
    while IFS=$'\t' read -r group name; do
        [ -n "$group" ] || continue
        sl_run_case plain "$group" "$name"
        if [ "$SL_RC" -eq 0 ]; then
            SL_PASS[$group]=$((SL_PASS[$group] + 1))
        else
            echo "FAIL $group.$name (rc=$SL_RC)"
            grep -v '^\[FAIL\]' "$SL_LOG" | head -n 6 | cut -c1-240
            if [ "$SL_RC" -eq 124 ] || [ "$SL_RC" -eq 137 ]; then
                SL_PLAIN_TIMEOUT[$group.$name]=1
            fi
        fi
    done < "$SL_BUILD_DIR/cases.txt"

    # sanitizer 组。
    for variant in ${SL_SANITIZERS:-}; do
        [ -n "${SL_TOTAL[$variant]:-}" ] ||
            sl_die "SL_SANITIZERS 含 $variant 但 SL_GROUPS 没有声明"
        if [ "$(cat "$SL_BUILD_DIR/compile_$variant.rc" 2>/dev/null)" != "0" ]
        then
            echo "$variant 构建失败，该组记 0："
            head -n 20 "$SL_BUILD_DIR/compile_$variant.log" | cut -c1-240
            continue
        fi
        if sl_sanitizer_clean "$variant"; then
            SL_PASS[$variant]=1
        else
            echo "FAIL $variant（存在 sanitizer 报告、崩溃或超时）"
        fi
    done

    sl_emit true
    exit 0
}
