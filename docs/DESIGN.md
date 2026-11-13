# agent-router 设计

目的：给 Claude（编排者）派活用的执行器池做路由、记账、准入评测和规则自迭代。
目标是提高效率：便宜快的模型做能做的事，贵的模型只做必须做的事，
规则随真实数据和新模型自动更新。

## 1 范围与权限

| 动作 | 自主程度 |
|---|---|
| 写账本、检测新模型、跑准入评测、出报告 | 全自动 |
| 修改 `config/models.yaml` 内的路由顺序和门槛参数 | 自动生效，事后通知用户 |
| 修改 `~/.claude/CLAUDE.md`、各仓库 `AGENTS.md` 的规则条文 | 先给用户看，确认后再改 |
| 修改判定规则本身（本文第 6 节） | 先给用户看，确认后再改 |

判定规则不由评测结果反向修改，防止自我刷分。

## 2 执行器池

| 来源 | 调用方式 | 当前成员 |
|---|---|---|
| ChatGPT / Codex | `codex exec -m <slug> ... </dev/null` | gpt-6-luna、gpt-6.1-sol；候选：gpt-6.1-terra（gpt-6-astra 用户 2026-11-13 移出） |
| Claude Code | `claude -p --model <id> --output-format json` | 只用 haiku（用户 2026-11-13 决定；sonnet/opus/fable 不做执行器） |
| 兜底 | `dsh-exec`（deepseek-flash / high） | 只在池内所有模型额度用完时使用，不参与路由排序 |

新的 Haiku 版本同样走准入评测。

## 3 任务类别

| 代号 | 定义 |
|---|---|
| A 清晰小任务 | 规格清楚、单模块、测试能完全验收 |
| B 大代码库 / 歧义 | 改已有较大代码、需求有歧义、跨模块 |
| C 安全 / 并发语义 | 联锁、状态机交互、线程安全、外部输入校验 |
| D 规范遵守 | 命名、格式、注释、测试文件等团队规范（作为 A–C 的附加评分，也可单独出题） |

派活前由 Claude 标注类别；一个任务属于多类时取最严的（C > B > A）。

## 4 文件布局

```
agent-router/
  config/models.yaml        # 模型池、各类路由顺序、返工门槛、判定阈值
  ledger/ledger.jsonl       # 真实任务账本，只追加
  bench/tasks/<id>/         # 准入基准题：任务说明、起始代码、隐藏测试
  bench/splits.yaml         # dev / holdout 划分
  bench/results/<run>/      # 每次评测输出，不覆盖
  tools/                    # ledger、detect、bench runner、report 等脚本
  reports/                  # 周报和判定报告
  docs/DESIGN.md
```

## 5 数据

### 5.1 账本记录（每次派活一条）

字段：`ts`、`task_id`、`project`、`category`、`executor`（codex/claude/dsh）、
`model`、`attempt`（第几次交付，首次为 0）、`outcome`
（pass / fail_test / fail_review / regression / test_gaming / misunderstood /
quota / env_error）、`escalated_to`、`wall_s`、`tokens_in`、`tokens_out`、
`cost_usd`（拿不到填 null）、`note`。

### 5.2 新模型检测

- Codex：读 `~/.codex/models_cache.json` 的 `models[].slug`，`visibility == "list"`
  且不在 models.yaml 中的视为新模型。
- Claude：比对 models.yaml 中登记的 Claude 模型 ID 与`haiku` 别名当前解析到的 ID。
  获取方式（2026-11-13 实测）：`claude -p --model <alias> --output-format json "只回复 OK"`，
  输出 JSON 的 `modelUsage` 键即实际模型 ID（haiku → claude-haiku-5-5，sonnet → claude-sonnet-5-5）；
  `total_cost_usd` 为花费，`session_id` 用于续接。别名解析到的 ID 变了即视为新模型。

## 6 判定规则（预先固定）

### 6.1 准入基准

- 每类至少 3 题，每题每模型跑 3 次；推理强度统一（Codex 用 medium）。
- 评分：隐藏测试通过率；C 类另加 TSan/ASan 和安全用例；D 类为规范检查项通过率。
- 过程维度：是否编写测试、是否越界改动或削弱已有测试、汇报是否诚实，定义见 bench/INTERFACE.md。
- 记录耗时、tokens、花费。

### 6.2 新模型进入某类路由

在该类 holdout 题上同时满足：
1. 通过率 ≥ 该类现任首选模型；C 类要求安全用例 100% 通过。
2. 中位耗时 ≤ 现任的 0.7 倍，或单题花费 ≤ 现任的 0.5 倍。

3. 该类所有运行中没有一次谎报测试结果（REPORT.md 声称 PASS 但模型自己的测试编译失败或不通过）；出现一次即 reject（用户 2026-11-13 同意）。

满足则排到现任前面；只满足 1 则排在现任后面作为备选；不满足 1 不进入。
dev 题可用于调提示词，holdout 只在最终判定时跑一次。

### 6.3 现任模型复评触发

某类最近 20 条账本中（不足 8 条不判定），升级率或返工率 > 30%，触发该类复评。
账本数据只触发复评，不直接改路由（样本太小）。

### 6.4 返工门槛

初始值：同一缺陷 2 轮、累计 3 轮升级；回归 / 糊弄测试 / 派错类别立即升级。
当某类积累 ≥ 20 条有返工的记录后，按"第 N 轮返工后最终通过的比例"调整：
若第 N 轮挽回率 < 15%，门槛降到 N−1。

## 7 触发

- 每周一次：检测新模型、生成账本周报、检查 6.3。
- 发现新模型：自动跑准入评测并按 6.2 判定。

## 8 风险

- Claude 执行器消耗与编排者相同的订阅额度；周报里单列 Claude 执行器用量。
- 样本小，统计弱；所有判定报告写明样本数。
- 评测在独立目录进行，不碰业务仓库。
- 基准题需定期补充，防止模型或提示词对题目过拟合。

## 9 里程碑

- M1：models.yaml、账本写入/查询工具、新模型检测、单测。
- M2：重建准入基准（A–C 各 3 题，D 为附加评分）与 runner；整个评测器由 Claude Fable 5.2（high）负责（用户 2026-11-13 指定，Fable 不在被测池内，避免出题偏向）。
- M3：周报与判定报告、6.2–6.4 自动化、接入每周触发。
