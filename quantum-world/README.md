# Quantum World

本地量子电路搜索环境，供 `OpenQuantum/star-agent-runtime` 通过 Star Hub 调用。agent 编写生成器、运行官方验证、读取成本、选择候选；不微调模型权重。

```text
自研 agent → Runtime Action → Star Hub → Quantum World
                                       ├─ SQLite 候选、实验、Action 账本
                                       ├─ Docker 执行候选生成器
                                       └─ Python 3.12 官方预检、验证器
三项完整验证 → 比较本地估分 → submission.zip → 等待人工审批
人工确认 SHA-256 → 重查线上最高分 → 单次上传 → 保存回执
```

## 本机启动

以下命令从本目录执行。World 使用 Python 3.13，官方验证器复用上级项目的 Python 3.12 `.venv`。这两个环境独立，SDK 与数值验证依赖不会混装。

```bash
uv sync --locked --python 3.13
docker build -t quantum-world-generator:local .
cp config.example.toml config.toml
.venv/bin/quantum-world --config config.toml call GetTask --params '{"task":"invsqrt"}'
.venv/bin/quantum-world --config config.toml serve --prompt-token
```

`serve` 需要已有 Star Hub；工具在加入同一 Hub/World 后动态发现。Hub token 由终端隐藏输入或 `HUB_CLIENT_TOKEN` 提供。这里没有新建 Hub、连接生产 Hub、启动付费模型或保存凭据。Docker Desktop 需处于运行状态。

本机系统代理为 `127.0.0.1:10080` 时，构建可使用以下命令；代理仅在构建时使用，候选容器运行时禁网。

```bash
HTTPS_PROXY=http://127.0.0.1:10080 uv sync --locked --python 3.13
docker build --build-arg HTTP_PROXY=http://host.docker.internal:10080 \
  --build-arg HTTPS_PROXY=http://host.docker.internal:10080 -t quantum-world-generator:local .
```

`config.toml` 中默认 seed 是已在本机保留的优化组合 `../research/candidates/rotated50`；来源、许可及已有验证见上级 `research/README.md`。可以改为官方 `../official/data/sample_submission`。seed 是操作员配置的只读起点，agent 不能指定任意宿主机输入路径。`current_best=25.08686` 是历史已知门槛，不代表已经实时查询。上传前必须重新查询账号全部历史，并等待所有待评分提交完成。

单机默认同时执行一个实验，验证器最多 2 个 worker；生成器限制 2 CPU、3 GiB、300 秒，完整验证超时 3600 秒。磁盘保留检查为 12 GiB，至少保留 3 GiB 空闲空间；没有自动清理历史实验。输出目录限制 2 GiB、子进程日志限制 4 MiB，以周期检查实施，存在短暂超量窗口。持续搜索前由操作员清理不再需要的历史实验。

## Runtime 配置

在 Runtime 自己的配置中合并下列字段，保留已有模型配置。不要直接覆盖已有私有配置。`human_client_id` 必须填写实际 BFF/Human 端点，`allowed_user_id` 必须与经过认证的输入一致；World 的 `hub.allowed_agents` 也要使用相同的 agent/user ID。

```toml
[hub]
url = "ws://127.0.0.1:8000"
agent_id = "quantum-agent-local"
world_id = "quantum-world-local"

[human]
enabled = true
human_client_id = "REPLACE_WITH_ACTUAL_HUMAN_CLIENT_ID"
allowed_user_id = "local-user"

[world_tools]
enabled = true
action_timeout = 30.0
max_result_bytes = 65536
```

World 固定 SDK 为 star-hub `c237172f788412a93a3647129572b546ca949603`，与本地干净 Runtime checkout 使用同一提交。不会修改另一个有本地改动的 Runtime 工作区。

## agent 工具与交互

| 工具 | 关键参数 | 返回/用途 |
|---|---|---|
| GetTask | task | 约束、参考成本、门槛 |
| CreateCandidate | task, files, entrypoint | 不可变 candidate_id |
| RunExperiment | task, mode, candidate_id 可省略 | 立即返回 experiment_id；省略候选即测试 seed |
| GetExperiment | experiment_id | 排队/运行/成功/失败、成本、有限日志 |
| CancelExperiment | experiment_id | 取消排队工作或终止进程组/容器 |
| BuildSubmission | 三个 experiment ID | 三项 full 成功且超过门槛后生成包，状态 awaiting_approval |

`task` 为 `invsqrt`、`dqi`、`dfthcsos`，`mode` 为 `quick` 或 `full`。Runtime 自动补齐 `userId`，模型不生成该字段。使用 SDK Action/Outcome，每个 Outcome 只有 `data.text`，其中是 JSON 字符串。`completed` 仅指这次工具请求完成；实验是否成功必须看 `GetExperiment.status`。轮询每次使用新的 Action ID；同一 ID 重发只重放首次回执，绝不再次执行。

生成器入口接收 `--output-dir /out`，目录中预先复制了所选任务的 seed 文件。例如：

```python
CreateCandidate(task="invsqrt", files={"main.py": "...完整 Python 代码..."}, entrypoint="main.py")
RunExperiment(task="invsqrt", candidate_id="cand-...", mode="quick")
GetExperiment(experiment_id="exp-...")
# 通过后对同一候选再跑 mode="full"，三个任务均 full 通过后打包。
BuildSubmission(experiments=["exp-invsqrt-...", "exp-dqi-...", "exp-chemistry-..."])
```

上述为交互示意，不是可直接运行的 Python SDK。可运行生成器示例为 `examples/identity.py`，它使用 kickmix 读写 seed 电路，保持语义。候选文件单个最多 64 KiB、总共 256 KiB，支持多个相对路径文件。

输出格式：invsqrt/dqi 分别一个同名 `.kmb` 或 `.kmx`；化学任务为连续编号的 `angle_k_0...N.kmb/kmx` 与 `submission.npz`。更换电路格式时需删除旧格式，避免同时存在两个同名电路。官方预检检查门集、寄存器、数组类型与形状；官方验证器检查正确性、相位及成本。

quick：逆平方根只测试前 4096 个输入；DQI 4096 shots；化学每个地址 1 shot。quick 的 `task_cost` 为 null，`diagnostic_cost` 仅作诊断，永远不能打包。full 不减少官方默认测试量，不修改验证器。每个实验使用独立命名 salt，不能复现 Kaggle 秘密 salt。

成本越低越好。合并估分为三个 `reference_cost / task_cost` 的几何平均；参考成本来自已完整验证的本地官方基线。Kaggle 使用秘密 salt 对应的参考成本，本地估分与真实榜单分可能不同，不能据此保证线上提升。

## 人工审批和上传

agent 工具目录没有 approve/submit；候选容器无网络，不挂载 World 数据库、Kaggle 凭据或 Docker socket。以下命令仅由人工操作员在宿主机执行，可以与 World 守护进程并行。审批命令要求交互终端，展示包后必须逐字输入 `APPROVE <完整 SHA-256>`，没有 `--yes` 或 agent 可调用的审批工具。

```bash
.venv/bin/quantum-world --config config.toml inspect-package pkg-...
# 检查 data_root/experiments/<id>/verify.stdout 及 packages/<id>/review.json。
.venv/bin/quantum-world --config config.toml approve pkg-... --operator YOUR_NAME
# 此后仍需人工显式运行；凭据仅通过 Kaggle 标准配置/操作员环境变量提供。
.venv/bin/quantum-world --config config.toml submit pkg-...
```

审批绑定包哈希，24 小时有效且仅能使用一次。文件改变、审批过期、线上分数未提升或线上存在待评分提交，均停止上传。审批过期后可由三个已有完整实验重新打包，再人工审核。上传前持久化 `submitting` 并消费审批；上传异常或进程崩溃标记 `unknown`，不自动重试，必须先在 Kaggle 核对是否已经创建提交。保存 `receipt.json` 仅表示创建成功，最终分数仍需在 Kaggle 查看。历史 `scripts/kaggle_submission.py submit` 同样加入了人工终端确认。

这是本地操作员与受限 agent 之间的权限分离。请勿把宿主机 shell、审批数据库的写权限或操作员凭据交给 agent。完整验证器作为可信官方程序在宿主机子进程运行；候选 Python 只在 Docker 中运行。

## 验证和代码位置

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src ../integrations/star-agent-runtime/.venv/bin/python -m unittest discover -s tests_runtime -v
.venv/bin/python examples/verify_baseline.py --docker --mode quick --tasks invsqrt
# 三项完整基线验证本机约 17 分钟，不会访问 Kaggle。
.venv/bin/python examples/verify_baseline.py --mode full --tasks invsqrt dqi dfthcsos
```

2026-11-06 本机验证：26 项单元测试与 1 项 Runtime Action/World 往返测试通过；往返测试仅替换网络 I/O，使用真实 Runtime、工具桥、World 和 SDK Schema，不调用模型。Docker 中运行生成器后，官方快速验证通过。三项默认完整验证全部通过，原始记录见 `validation/baseline-full.json`；官方基线未超过配置门槛，打包返回 `NO_IMPROVEMENT`。未执行真实 Kaggle 上传，也未做生产 Hub 或付费模型的端到端验收。

| 文件 | 职责 |
|---|---|
| src/quantum_world/protocol.py | Star Environment、回执与拒绝重放 |
| src/quantum_world/service.py | 工具、候选、队列、打包 |
| src/quantum_world/runner.py | Docker 生成与官方子进程 |
| src/quantum_world/preflight.py | Python 3.12 官方 metric 预检 |
| src/quantum_world/store.py | SQLite 所有权与 Action 幂等记录 |
| src/quantum_world/approval.py | 人工审批状态机 |
| src/quantum_world/gateway.py | 操作员 Kaggle 子进程桥 |
| src/quantum_world/cli.py | 本地调用、守护进程与管理命令 |

重启不自动重放正在执行的实验，标记为 `interrupted`；由 agent 新建实验。World 数据目录有独占锁，同一目录只能运行一个执行服务；审批管理使用独立操作员锁。SQLite 账本不是登录系统，业务身份依赖已认证 Hub 的 sender 与 World 精确 allowlist。
