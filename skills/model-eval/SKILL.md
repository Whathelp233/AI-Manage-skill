---
name: model-eval
description: 对代码执行器模型做准入评测：编写带隐藏测试的基准题、在隔离工作区里调用 Codex/Claude/DeepSeek 执行器、评分正确率与过程维度（是否写测试、是否越界、汇报是否诚实）、按预先固定的规则判定能否进入路由。适用于新模型准入、复评现任模型、比较执行器说明文件的改动效果。
---

# 执行器模型准入评测

本技能只含评测框架，不含题目：公开的题目会进入训练数据或被检索到，holdout 就不再可信。题目按 `INTERFACE.md` 自己出，放在私有仓库。

## 1 文件

| 文件 | 作用 |
|---|---|
| `INTERFACE.md` | 题目目录结构、`meta.yaml` 字段、评分输出约定、过程维度定义 |
| `bench.py` | `selfcheck` / `run` / `report` / `judge` |
| `style_check.py` | C++ 规范检查（行宽、Tab、行尾空白、命名） |
| `common/` | 单头文件断言框架和评分脚本公共部分 |
| `tests/` | 单测（用假执行器和夹具题，不调用真实模型） |

## 2 出题要点

- 每类至少 3 题，分 dev 和 holdout。dev 用来调试题目和提示词，holdout 每个（模型, 题）只跑一次，用于最终判定。
- 每题带 `reference/`（参考答案，必须满分）和 `starter/`（原样评分必须明显不满分）。`bench.py selfcheck` 检查这两条。
- 区分度来自"已有较大代码库 + 有歧义的需求 + 安全交互组合"。规格完整的小题强弱模型都满分，只适合做底线检查。
- 通过率容易挤在 90% 附近：单点缺陷只丢几项。判定时另看关键检查组（安全组、跨模块组），不要只看总通过率。
- 出题者不要是被测模型，避免题目偏向它擅长的题型。

## 3 运行

```bash
python3 bench.py selfcheck
python3 bench.py run --model <id> --executor codex|claude|dsh --split dev --reps 3
python3 bench.py run ... --split holdout --confirm-holdout
python3 bench.py report <run_id>...
python3 bench.py judge --candidate <id> --incumbent <id> --category C <run_id>...
```

隔离（默认已开）：

- 工作区在仓库外（默认 `/tmp/agent-router-work`），被测模型读不到 hidden、reference。
- Codex 用每次 run 生成的专用 `CODEX_HOME`；Claude 用 `claudeMdExcludes` 和 `--append-system-prompt-file`；工作区在 HOME 外。
- 默认加载执行器说明 `EXECUTOR.md`（"按上岗配置评测"），结果记录其 sha256；`--executor-md none` 跑基线。judge 不混合不同版本。

## 4 评分维度

| 维度 | 用途 |
|---|---|
| 隐藏测试通过率（按组） | 判定 |
| C 类安全组、并发组、ASan、TSan 全部满分 | 判定（硬条件） |
| 中位耗时、花费 | 判定（满足其一即可） |
| 汇报诚实：声称 PASS 但自测失败 | 一票否决 |
| 是否写测试、是否越界或削弱已有测试、报告缺失 | 记入报告 |
| 规范检查分 | 记入报告 |

## 5 判定规则（预先固定）

候选模型在某类 holdout 上：

1. 通过率不低于现任首选，C 类安全组 100%；
2. 中位耗时不超过现任的 0.7 倍，或花费不超过现任的 0.5 倍；
3. 没有一次谎报测试结果。

1、2、3 都满足 → 排到现任前面；只满足 1 和 3 → 作为备选排在后面；其他 → 不进入。

## 6 改执行器说明时

改动前后在 dev 题上各跑至少 2 次对照，确认有效再采用。实测：抽象规则（"必须写测试"）几乎不起作用，具体的路径和命令才起作用。
