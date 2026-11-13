# AI-Manage-skill

三个可独立安装的 skill，用于向代码执行器派活、选择模型和运行准入评测。

| Skill | 作用 |
|---|---|
| executor-dispatch | 任务分类、执行器调用、指令隔离与验收 |
| model-routing | 模型池、路由查询、私有账本与复评触发 |
| model-eval | 私有题目接口、评测框架、过程评分与准入判定 |

依赖：python3、PyYAML（`python3 -m pip install PyYAML`）；C++ 评分需要
g++、make、bash、timeout，TSan 另需 setarch。单测使用假执行器，不调用真实模型。
实际派活时另安装所选的 Codex CLI、Claude Code 或 DeepSeek Harness。

## 安装与使用

把所需的 `skills/<名称>/` 完整复制到项目的 `.claude/skills/` 或
`.agents/skills/` 下，保留各 skill 内部文件结构。阅读其 `SKILL.md`。
以下命令从本公开仓库根目录执行；安装后相应替换 skills 路径。

```sh
skills/executor-dispatch/scripts/setup_codex_exec_home.sh
mkdir -p skills/model-routing/config
cp skills/model-routing/models.example.yaml skills/model-routing/config/models.yaml
python3 skills/model-routing/router.py route A
python3 skills/model-routing/router.py --config /private/models.yaml \
    --ledger /private/dispatch.jsonl stats
python3 skills/model-eval/bench.py --bench-dir /private/bench selfcheck
python3 skills/model-eval/bench.py --bench-dir /private/bench run \
    --executor codex --model MODEL --split dev \
    --executor-md /absolute/path/to/executor-dispatch/EXECUTOR.md
python3 skills/model-eval/bench.py --bench-dir /private/bench judge \
    --config /private/models.yaml --candidate NEW --incumbent OLD --category A RUN_ID
python3 -m unittest discover -s skills/model-routing/tests -p 'test_*.py'
python3 -m unittest discover -s skills/model-eval/tests -p 'test_*.py'
```

router 默认读取所在 skill 的 `config/models.yaml`，账本写入其 `ledger/`；
bench 默认题库为所在 skill 的 `bench/`（请用 `--bench-dir` 指向私有题库），
默认配置读取相邻 model-routing 的 `config/models.yaml`，默认执行器说明读取
相邻 executor-dispatch 的 `EXECUTOR.md`。路径参数允许移动或独立安装 skill。
bench 的相对 `--executor-md` 路径以脚本所在 skill 为基准，请优先传绝对路径；
run 示例中的 EXECUTOR.md 请换成安装后的实际绝对路径。
`--executor-md none` 用于不加载说明的基线评测。

setup 脚本接受 `[--force] [目标目录 [EXECUTOR.md路径]]`，默认目标为
`~/.codex-exec`，默认说明文件位于该 skill 内。认证从
`${CODEX_HOME_SRC:-~/.codex}/auth.json` 链接，认证源必须已存在；已有文件默认保留。

## 隔离与公开边界

执行器专用 CODEX_HOME 只链接认证和 EXECUTOR.md，最小配置只设置 medium
推理强度，避免继承编排者的全局指令。评测工作区位于私有题库之外，只提供
起始代码和任务说明；隐藏评分材料在执行器结束后才加入评分副本。
Claude 工作区须位于 HOME 之外，并排除宿主全局编排规则。
这些机制隔离指令与材料；需要更强的恶意代码防护时应另用系统沙箱。

本仓库仅发布工具、接口、说明及合成测试夹具。真实题目、账本和评测结果始终私有。
agent-router 是唯一源头：所有公开文件均由其 `tools/export_public.py --out DIR`
按逐文件白名单生成，不在公开副本中维护源代码。脚本检查禁止路径与敏感字符串，
并在导出目录运行两个 skill 的单测；任一失败即退出非零。
`--force` 清理目标中的旧文件，仅保留根目录已有的 `.git` 元数据目录；该目录
不参与发布内容扫描。导出脚本不执行 git，不联网，也不发布到 GitHub。

License: MIT（见 LICENSE）

设计背景见 [docs/DESIGN.md](docs/DESIGN.md)，题目接口见
[INTERFACE.md](skills/model-eval/INTERFACE.md)。DESIGN.md 保留源仓库布局的描述。
