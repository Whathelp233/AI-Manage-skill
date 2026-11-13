---
name: model-routing
description: 维护执行器模型池和按任务类别的路由表，记录每次派活的账本，检测新模型，按账本触发复评。适用于"该派给哪个模型""记一条派活结果""有没有新模型""路由要不要调整"。
---

# 模型路由与账本

## 1 文件

| 文件 | 作用 |
|---|---|
| `models.example.yaml` | 模型池、各类路由顺序、返工门槛、判定阈值（复制为 `config/models.yaml` 使用） |
| `router.py` | 路由查询、账本、统计、新模型检测、复评检查 |
| `tests/test_router.py` | 单测：`python3 -m unittest discover -s tests -p 'test_*.py' -v` |

只依赖 Python 标准库和 PyYAML。

## 2 常用命令

```bash
python3 router.py route A                     # A 类路由顺序（跳过非 active 模型）
python3 router.py log --task-id T1 --category A --executor codex \
    --model gpt-x --attempt 0 --outcome pass --wall-s 120
python3 router.py stats --category B --last 20
python3 router.py detect                       # 读 ~/.codex/models_cache.json
python3 router.py detect --claude-ids claude-haiku-x-y
python3 router.py check-reeval                 # 哪些类别需要复评
```

`--config`、`--ledger` 可覆盖默认路径。

## 3 账本字段

`ts`、`task_id`、`project`、`category`（A/B/C）、`executor`（codex/claude/dsh）、`model`、`attempt`（首次交付为 0）、`outcome`（pass / fail_test / fail_review / regression / test_gaming / misunderstood / quota / env_error）、`escalated_to`、`wall_s`、`tokens_in`、`tokens_out`、`cost_usd`、`note`。

账本只追加，不改写。记录的是实际执行的模型（例如 resume 退回了默认模型，就记默认模型）。

## 4 新模型检测

- Codex：`~/.codex/models_cache.json` 中 `visibility == "list"` 且不在池里的 `slug`。
- Claude：`claude -p --model <别名> --output-format json "只回复 OK"`，输出 JSON 中 `modelUsage` 的键就是别名当前对应的实际 ID；与池中登记的 ID 不同即为新模型。

发现新模型后用 `model-eval` 技能做准入评测，不要直接加进路由。

## 5 复评触发

某类最近 20 条记录中（不足 8 条不判定），升级率或返工率超过 30% → 该类复评。
账本只触发复评，不直接改路由：样本太小，直接改容易被偶然波动带偏。

## 6 权限分档（建议）

| 动作 | 自主程度 |
|---|---|
| 记账、检测、评测、出报告 | 全自动 |
| 按评测结果调整路由顺序和门槛参数 | 自动生效，事后通知用户 |
| 修改编排规则、执行器说明、判定规则 | 先经用户确认 |

判定规则不能由评测结果反向修改，否则会自我刷分。
