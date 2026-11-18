"""py_mt.py：Python 题隐藏测试用的最小用例注册框架（对应 C++ 的 mini_test.h）。

隐藏测试模块这样写：

    from py_mt import case, check

    @case("regression")
    def normal_corner_accurate():
        check(result_ok, "说明")

一个 @case 函数是一个计分项；函数正常返回为通过，抛出任何异常为失败。
由 py_score.py 逐个用例起子进程运行，见该文件说明。
"""

CASES = []


def case(group):
    """把函数登记为 group 组的一个用例，用例名取函数名。"""
    def register(function):
        CASES.append((group, function.__name__, function))
        return function
    return register


def check(condition, message=""):
    """条件不成立时让用例失败。"""
    if not condition:
        raise AssertionError(message or "check failed")
