---
name: executor-dispatch
description: 编排者把编码任务派给外部执行器（Codex CLI、Claude Code Haiku、DeepSeek Harness）的方法：任务分类、选模型、隔离指令、返工门槛、升级与记账。适用于"Claude 只编排、执行交给别的模型"的工作方式，派活、返工、换模型、执行器行为异常时先读。
---

# 执行器派活

编排者负责理解需求、拆任务、写指令、验收；执行器负责写代码、改文件、跑命令。本技能说明怎么派、怎么验、什么时候换人。

## 1 两份说明文件，各读各的

| 会话 | 读 | 不读 |
|---|---|---|
| 编排者 | 用户级 `~/.claude/CLAUDE.md`（编排规则） | `EXECUTOR.md` |
| 执行器 | `EXECUTOR.md`（本技能目录下）+ 项目自己的 AGENTS.md | 编排规则、`~/.codex/AGENTS.md` |

不隔离时会出现两类问题（均实测过）：

- 被派活的 Claude 读到"Claude 只编排、不写代码"，于是拒绝动手或停下等确认。
- Codex 读到全局 AGENTS.md 里"可委派给子代理"，把活转给别的模型，结果不是你选的模型做的。

## 2 调用方式

先运行一次 `scripts/setup_codex_exec_home.sh`，生成执行器专用的 `CODEX_HOME`（全局指令 = EXECUTOR.md，认证链接到 `~/.codex/auth.json`）。

### Codex

```bash
CODEX_HOME=~/.codex-exec python3 <本技能目录>/exec_guard.py codex -- \
  -C <项目目录> -m <模型> \
  -c model_reasoning_effort="<强度>" -s workspace-write \
  -o <临时目录>/codex-last.md "<任务指令>" </dev/null
```

- 强度取 `router.py route <类>` 输出的第二列；resume 时同样带上该强度。
- 通过 `exec_guard.py` 运行：Codex（0.167 实测）无法用 `--disable multi_agent` 或 `agents.max_depth=0` 关掉子代理，EXECUTOR.md 的"不要转派"也不总被遵守。exec_guard 监视 `$CODEX_HOME/sessions`，出现本次运行的子线程（首行 `thread_source: "subagent"`）即终止进程组并退出 86。`--` 之后是 `codex exec` 的参数，不要再写 `exec`。
- 必须 `</dev/null`，否则进程会一直等 stdin。
- 必须显式 `-m`：不写时使用 `config.toml` 的默认模型，可能是最贵的组合。
- 非 git 目录加 `--skip-git-repo-check`。
- 返工用 `codex exec resume <session-id>`，**不要用 `--last`**（可能抢到桌面版正在用的会话）。resume 不保留原会话的模型，也不接受 `-s`：必须加 `-c model="<模型>" -c model_reasoning_effort="<强度>" -c sandbox_mode="workspace-write"`，并看日志头的 `model:` 行确认。
- 沙箱里 `.git/` 只读：提交由编排者在宿主执行。

### Claude Code（Haiku）

```bash
cd <项目目录> && claude -p --model haiku --output-format json \
  --permission-mode auto --setting-sources project \
  --settings '{"claudeMdExcludes":["'"$HOME"'/.claude/CLAUDE.md"]}' \
  --append-system-prompt-file <本技能目录>/EXECUTOR.md \
  --disallowedTools "Agent,Task" \
  -- "<任务指令>" </dev/null > <临时目录>/claude-last.json
```

- `claudeMdExcludes`：项目在 HOME 下时，Claude Code 会把 `~/.claude/CLAUDE.md` 当作祖先目录的项目指令加载，`--setting-sources project` 挡不住。
- `--permission-mode auto`：`acceptEdits` 下 Bash/Read/Write 会被拒，执行器无法编译和自测。`--bare` 不走订阅登录。
- `--disallowedTools "Agent,Task"`：禁止执行器再叫子代理。必须用逗号形式并在任务指令前加 `--`：这个参数接受多个值，写成 `--disallowedTools Agent Task "<任务指令>"` 会把任务指令也当成工具名，执行器收不到任务，否则结果里会混进别的模型（验收时看输出的 `subagent_stats.spawned` 和 `modelUsage` 是否只有所派模型）。
- 输出 JSON：实际模型 ID 是 `modelUsage` 的键，花费 `total_cost_usd`，续接用 `session_id`。

### DeepSeek Harness（额度兜底）

把 EXECUTOR.md 内容拼在任务文件开头，然后：

```bash
dsh-exec <项目目录> --json - < task.txt > dsh-last.jsonl
```

成功 = 退出码 0 且出现 `{"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}}` 事件（`turn_end` 是 `status` 事件的 `phase`，不是独立的 `type`）；`final` 事件不能当作成功依据。

### 通用

- 耗时长的任务放后台运行，完成后再读结果，不要轮询。
- 执行器沙箱需要联网或越出工作区时先问用户，不要擅自放开沙箱（如 `danger-full-access`）。

## 3 任务分类与选模型

| 类 | 定义 | 选模型 |
|---|---|---|
| A | 规格清楚、单模块、测试能完全验收 | 路由表 A 类顺序（便宜快的在前） |
| B | 改较大的已有代码、需求有歧义、跨模块 | 路由表 B 类顺序 |
| C | 安全联锁、并发、状态机交互、外部输入校验 | 路由表 C 类顺序 |

一个任务属于多类时取最严的（C > B > A）。路由表由 `model-routing` 技能维护，依据 `model-eval` 技能的评测结果。

## 4 写指令

- 写目标、相关文件、约束、验收标准（具体命令）。
- 写具体路径和命令，不写原则。实测：模型对"必须写测试"这类抽象规则不响应，对"测试放在 `test/`，用 `make test` 运行"响应。
- 豁免条款用排除式（"只有 X 可以省略"），不要用包含式（"小任务可以省略"）——包含式会被最大化利用。

## 5 验收

- 不看执行器自己的结论，看实际结果：亲自重跑测试、读 diff。需要第二意见时可以用 `codex exec review` 做代码审查。
- 报告第一行 `TESTS: PASS|FAIL|NOT_RUN` 要和你重跑的结果一致。
- 查是否转派：执行器有没有调用子代理，或在 shell 里调用 `codex`、`claude`、`dsh-exec` 等别的模型 CLI。EXECUTOR.md 禁止转派，但只靠说明不够，要看输出确认。
- 重点查：是否写了测试；是否改了范围外文件；是否删除或削弱已有测试；测试是否自包含（连续跑两次都通过，不写真实路径）。

## 6 返工与升级

- 同一缺陷 2 轮没修好，或整个任务累计 3 轮没通过 → 升级到该类下一个模型。
- 以下情况立即升级：修复时改坏了原来通过的测试；改测试、删断言让测试变绿；返工中发现任务其实属于更严的类别。
- 升级时新开会话，带上失败记录和发现的问题，并说明可以推翻前一个方案。
- 该类所有模型都做不好时，停下来找用户。
- 额度用完才算切换到兜底执行器的条件；登录、沙箱、网络、指令问题不算。

## 7 记账

每次派活结束记一条（见 `model-routing` 技能的 `router.py log`）：任务、类别、模型、第几次交付、结果、耗时、花费。路由表的复评靠这些记录触发。

## 8 攒真实评测题

合成题对强模型很快饱和，真实失败任务才有区分度。C 类任务，以及任何返工过或升级过的任务，派活前在宿主记下起点：git 仓库记提交号（`git rev-parse HEAD`），非 git 目录打一个快照包（不含数据和二进制）。账本 note 里写上提交号或快照路径。验收通过后，修复前的起点加最终的测试就能改造成评测题（见 `model-eval` 技能）。
