# 本地 Runtime 工具联调

2026-11-12 下载并联调的两个仓库独立存放：

| 仓库 | 本地位置 | 已验证的提交 |
|---|---|---|
| quantum-circuit-compilation | `/Users/gwh/Documents/ChatGPT/cs/quantum-circuit-compilation` | `cda255ff88ae849b88e5e61fb8ee9d0b3a6cb146` |
| star-agent-runtime | `/Users/gwh/Documents/ChatGPT/cs/star-agent-runtime` | `57372eef2d5c1e614a3a0e299f1bb67f38af8153` |

旧目录 `../quantum-search` 中的未提交改动保持原样。本次仅复用其中的官方数据、metric 和 Python 3.12 验证环境作为测试输入，没有复制其中尚未提交的控制台代码。

## 已跑通的链路

```text
真实 Runtime Core / Action / WorldToolBridge
    → 公共 AgentClient
    → 本机 WebSocket Hub（固定 SDK 的 MessageRouter 测试服务）
    → QuantumEnvironment → World → 官方 invsqrt 快速验证
    → Outcome → Runtime Action → JSON 结果
```

六个工具全部动态发现；实际执行 GetTask、CreateCandidate、RunExperiment、GetExperiment。实验使用官方 seed，无 candidate_id，因此不执行候选容器。快速验证通过 262,144 shots，`full=false`、`task_cost=null`，不能用于提交。未运行模型决策、候选生成容器、生产 Hub 或 Kaggle 上传，也没有启动持久网页聊天服务。

最新 Runtime 的持久化已要求 PostgreSQL。联调脚本自动创建独立 PostgreSQL 17 临时容器，使用随机测试口令和回环地址，结束后删除容器；不复用环境中已有数据库连接。World 仍使用自己的 SQLite。两个仓库只通过协议通信，测试编排器从外部仓库加载 Runtime，不将其打包为 World 的运行依赖。

## 复现

从比赛仓库根目录运行，先启动 Docker Desktop：

```bash
uv sync --project ../star-agent-runtime --locked --python 3.13
uv sync --project quantum-bff --locked --extra test --python 3.13
uv run --project ../star-agent-runtime --no-sync --with uvicorn==0.54.0 \
  python scripts/smoke_runtime_tools.py \
  --runtime-repo ../star-agent-runtime --data-project ../quantum-search
```

如 GitHub 直连不可达，可在命令前设置本机代理 `HTTPS_PROXY=http://127.0.0.1:10080`。`--data-project` 必须包含 `.venv/bin/python`、`official/metric`、`official/data/verifiers` 和 `official/data/sample_submission/invsqrt.kmb`。测试只读这些已有文件，实验输出写入临时目录。

结果保存于忽略提交的 `.local-console/runtime-tools-smoke.json`。临时 Hub 只是接口测试夹具，不提供生产 Hub 的完整权限管理。`[human].enabled` 的真实聊天链还需按[控制台说明](console.md)配置实际 Hub、模型和 PostgreSQL；不要把这个烟测当成模型已经开始自主搜索。

跨目录测试存在同名 `test_console.py`，合并运行时使用 importlib 导入模式：

```bash
quantum-bff/.venv/bin/python -m pytest --import-mode=importlib \
  quantum-world/tests/test_protocol_contract.py quantum-world/tests/test_operator_api.py \
  scripts/tests quantum-bff/tests -q
node --test frontend/tests/model.test.js
```

## 下一步添加工具的位置

| 位置 | 修改内容 |
|---|---|
| `quantum-world/src/quantum_world/tools.py` | 工具名称、描述、JSON 参数 Schema；包含可信 userId |
| `quantum-world/src/quantum_world/service.py` | 工具分派、业务执行、所有权与 Action 幂等记录 |
| `scripts/console.py` 的 `TOOLS` | 若允许 BFF 直调该工具，更新其初始化授权目录；已有状态目录需单独更新 worlds.json 的 allowedTools |
| `quantum-world/tests` | 参数、权限、异步状态、重复请求及真实输出的验证 |

Runtime 的 `src/agent_runtime/modules/action/external/world_tools.py` 从 World 能力目录发现工具，无需把比赛工具列表硬编码进 Runtime。新增工具后重启 World 并刷新协商，使新的 agent run 读取新工具快照。长任务仍应立即返回 job ID，再通过状态工具轮询。审批与 Kaggle 上传继续由人工执行，不加入 agent 工具目录。

补充接口字段见[外部 Agent 接口](agent-interface.md)。

## 已连接的本机网页

2026-11-12 已在本机实际启动并通过 Chrome 端到端验收：

| 服务 | 地址 |
|---|---|
| 前端与 BFF | `http://127.0.0.1:8330/` |
| World 操作接口 | `http://127.0.0.1:8331/` |
| 独立认证 Hub | `ws://127.0.0.1:28080` |
| Runtime PostgreSQL | 仅本机端口 54329，容器 quantum-console-live-db |

旧服务的 8320/8321 端口未改动。网页登录用户为 `local-user`，口令在 `.local-console/live/login-code`；服务凭据和模型密钥仅保存在忽略提交的本机状态目录。模型沿用原有配置中的 `openai/gpt-6-luna`。比赛面板沿用只读配置，网页聊天可以由 Agent 发起实验；Kaggle 上传仍需人工审批。

启动器为 `scripts/local_stack.py`，在前台监督 Hub、World、Runtime、BFF 子进程，Ctrl+C / SIGTERM 停止这些服务；PostgreSQL 容器及数据卷保留，用于下次恢复。不要同时启动两个监督器。重新启动前确认 28080/8330/8331 端口可用：

```bash
quantum-bff/.venv/bin/python scripts/local_stack.py \
  --runtime-repo ../star-agent-runtime \
  --data-project ../quantum-search \
  --hub-repo /path/to/star-hub \
  --model-config /path/to/existing-runtime-config.toml
```

所有运行配置、日志和进程 PID 保存在 `.local-console/live/`。该本机启动器复用指定的 Hub 源码，不修改它，也不修改外部 Runtime 仓库。模型服务的本机代理目前使用 `127.0.0.1:10080`，需保持可用。

真实浏览器验收通过登录、GetTask、恰好一次 RunExperiment quick、GetExperiment 和实验日志显示，证据保存于 `.local-console/live/browser-evidence.json`。实验 `exp-ce7fcd8c84524530af027110eeb19b4a` 为 PASS，262,144 shots，`full=false`、`task_cost=null`，不构成完整比赛估分。复跑同一验收可复用状态文件，避免重复创建实验：

```bash
node scripts/tests/console_live_e2e.mjs --base-url http://127.0.0.1:8330 \
  --login-code-file .local-console/live/login-code \
  --state-file .local-console/live/browser-evidence.json \
  --playwright "$PWD/.local-console/live/test-tools/node_modules/playwright"
```
