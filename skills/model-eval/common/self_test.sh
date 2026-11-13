#!/usr/bin/env bash
# self_test.sh：运行工作区自带的测试（make test），最后一行输出
#   {"compile_ok": bool, "passed": int, "total": int}
# 退出码恒为 0。各题 meta.yaml 的 self_test_cmd 写成
#   bash "$BENCH_COMMON/self_test.sh"
# runner 负责把 BENCH_COMMON 设成 bench/common 的绝对路径；脚本放在工作区
# 之外，被测模型改不到。
#
# 粒度：task.md 约定 "make test 全部通过时退出码为 0，有失败时非 0"，所以
# 这里把整个 make test 当成 1 项：退出码 0 记 1/1，否则 0/1。失败时再按
# 输出里有没有编译器或链接器报错来区分 compile_ok，仅作参考。
set -u
timeout_s="${SELF_TEST_TIMEOUT_S:-240}"

if [ ! -f Makefile ] && [ ! -f makefile ] && [ ! -f GNUmakefile ]; then
    echo "工作区里没有 Makefile"
    echo '{"compile_ok": false, "passed": 0, "total": 1}'
    exit 0
fi

log="$(mktemp)"
timeout -k 5 "$timeout_s" make test > "$log" 2>&1 < /dev/null
rc=$?
tail -n 40 "$log" | cut -c1-240
if [ "$rc" -eq 0 ]; then
    result='{"compile_ok": true, "passed": 1, "total": 1}'
elif grep -Eq 'error:|undefined reference|ld returned|No rule to make target|fatal error' "$log"; then
    result='{"compile_ok": false, "passed": 0, "total": 1}'
else
    result='{"compile_ok": true, "passed": 0, "total": 1}'
fi
rm -f "$log"
echo "make test 退出码 $rc"
echo "$result"
exit 0
