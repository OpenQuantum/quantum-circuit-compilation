# Kaggle 量子电路比赛提交复现

本地 agent 交互环境已实现，入口见 [Quantum World](quantum-world/README.md)。新提交必须先由人工审批具体文件哈希，agent 不具备审批或上传工具。下面保留官方基线的历史复现记录；后续优化见 [research/README.md](research/README.md)。

本目录已跑通官方基线的打包、完整验证和 Kaggle 提交，并完成后续公开方案优化和本地 Quantum World 实现。当前采用本地部署，未部署服务器。下文的基线分数、回执与复现命令均为历史基线记录，不代表当前账号最高分。

## 代码结构

本项目包含官方评分代码、优化研究代码、Quantum World，以及独立验证与提交脚本；Agent Runtime 在外部仓库独立维护，通过公共协议连接。下面展开主要源码；批量电路、下载包、日志和依赖按目录归纳，省略各 Python 包的 `__init__.py`。

```bash
quantum-search/
├── README.md                          # 项目说明、结构、执行关系与基线记录
├── requirements-local.lock.txt        # 官方验证环境依赖，Python 3.12
│
├── official/                          # 官方比赛材料，评分逻辑保持原样
│   ├── metric/
│   │   ├── google-quantum-ai-metric.ipynb
│   │   └── google_quantum_ai_metric.py # 官方预检、验证调度、总分计算
│   └── data/
│       ├── verifiable_circuit_compilation.pdf
│       ├── kickmix/                   # 官方模拟器配套材料
│       ├── sample_submission/         # 官方基线电路和数值参数
│       │   ├── invsqrt.kmb
│       │   ├── dqi.kmb
│       │   └── dfthcsos/
│       │       ├── angle_k_*.kmb
│       │       └── submission.npz
│       └── verifiers/
│           ├── invsqrt/               # 逆平方根相位电路
│           │   ├── nr_reference_circuit.py
│           │   └── verify_submission_invsqrt.py
│           ├── dqi/                   # Reed–Solomon 解码电路
│           │   ├── dqi_reference_circuit.py
│           │   └── verify_submission_dqi.py
│           └── dfthcsos/              # 化学因子分解与角度加载
│               ├── SCORING_README.md
│               ├── verify.py
│               ├── FeMoCo76o113e.fcidump
│               ├── FeMoCo76o113e.fixed_shifts.npz
│               ├── example_submission/
│               │   └── angle_k_reference_circuit.py
│               └── scoreinfra/
│                   ├── reference.py
│                   ├── circuits.py
│                   ├── costs_prepare.py
│                   └── dfthc_math.py
│
├── scripts/                           # 最初的独立复现脚本
│   ├── preflight.py                   # 电路、数组格式预检
│   ├── verify_local.py                # 运行三项官方完整验证
│   ├── package_baseline.py            # 打包官方基线
│   └── kaggle_submission.py           # 历史提交/状态查询；提交需人工确认
│
├── research/                          # 开源方案分析和自有优化
│   ├── README.md                      # 来源、优化方法、分数与验证证据
│   ├── exact_rewrite.py               # 可交换门抵消、XOR 因式分解
│   ├── product_reuse.py               # 符号分析，复用已有布尔乘积
│   ├── rotate_weights.py              # 正交旋转 W、H2_I，优化化学成本
│   ├── submit_improvement.py          # 历史改进比较入口，调用人工确认脚本
│   ├── discussions/                   # Kaggle Discussion 检索快照
│   ├── sources/                       # 公开代码、数据、发布清单和许可
│   │   ├── heliosli/
│   │   ├── heliosli-release/
│   │   ├── syed/
│   │   ├── blend/
│   │   ├── current-best/
│   │   └── public-index/
│   ├── candidates/                    # 各阶段电路与数值参数
│   │   ├── incumbent/                 # 账号原有方案
│   │   ├── public-best/               # 下载的公开组合
│   │   ├── rewritten/                 # 门重写候选
│   │   ├── reuse/                     # 布尔乘积复用候选
│   │   ├── rotated/                   # 权重旋转候选
│   │   └── rotated50/                 # 当前 World 配置样例的默认 seed
│   ├── inspection/                    # 用于分析的文本电路
│   └── receipts/                      # 正确性、成本、比较和提交回执
│
├── quantum-world/                     # 新建的 agent 交互环境
│   ├── README.md                      # 安装、启动、接入与人工审批说明
│   ├── pyproject.toml                 # Python 3.13 包与命令入口
│   ├── uv.lock                        # World 依赖锁
│   ├── config.example.toml            # 路径、资源、身份、Hub 配置
│   ├── Dockerfile                     # 候选生成器运行镜像
│   ├── src/quantum_world/
│   │   ├── cli.py                     # 启动服务、本地调用、人工管理
│   │   ├── config.py                  # 配置加载与检查
│   │   ├── tools.py                   # 六个 agent 工具及参数 Schema
│   │   ├── protocol.py                # Star Protocol Action/Outcome 接入
│   │   ├── service.py                 # 候选、实验队列、查询、取消、打包
│   │   ├── runner.py                  # Docker 生成和官方验证子进程
│   │   ├── preflight.py               # 调用官方 metric 做输入预检
│   │   ├── store.py                   # SQLite、所有权、幂等与重启恢复
│   │   ├── approval.py                # 人工审批、有效期、哈希与单次提交
│   │   ├── gateway.py                 # 操作员进程到 Kaggle 的桥接
│   │   └── kaggle_gateway.py           # 查询最高分、分块上传、保存回执
│   ├── examples/
│   │   ├── agent_client.py            # 仅使用公共 SDK 的独立客户端
│   │   ├── identity.py                # Docker 内读写基线电路的生成器示例
│   │   └── verify_baseline.py          # 基线真实验证入口，不访问 Kaggle
│   ├── tests/
│   │   ├── test_world.py              # 业务、隔离、审批、进程等测试
│   │   └── test_protocol_contract.py  # 公共协议接口测试，无 Runtime 依赖
│   └── validation/
│       ├── baseline-full.json         # 三项完整验证记录
│       └── baseline-quick-docker.json # Docker 生成与快速验证记录
│
├── docs/
│   ├── agent-interface.md             # 外部 Runtime 接口、配置与联调
│   └── runtime-integration-audit.json # 历史审计记录，非运行依赖
│
├── runs/                              # 原始基线验证与提交记录
├── artifacts/                         # 已打包的提交文件
├── downloads/                         # 官方原始下载包
├── .quantum-world/                    # 正式 World 的 SQLite 等运行数据
└── .quantum-world-smoke/               # 本地验证实验数据
```

`official/` 提供判定标准；`research/` 提供优化算法与已有候选；`quantum-world/` 提供 agent 可交互的执行环境；模型决策和工具调用由外部 [star-agent-runtime 仓库](https://github.com/OpenQuantum/star-agent-runtime)负责；见 [外部接口](docs/agent-interface.md)。World 默认从配置的 seed 复制输入，已有研究脚本不会自动成为 agent 工具；agent 通过 `CreateCandidate` 提供生成器代码。

## 实际执行关系

正式接入使用下面的调用链。Star Hub 是独立服务，连接信息与身份由配置提供。

```text
自研 agent 决定下一步
    ↓
Runtime Action 模块发现工具、补齐可信 userId、记录并派发 Action
    ↓ Star Hub
protocol.py 接收请求 → service.py 校验身份和参数 → store.py 记录
    ↓ RunExperiment 立即返回 experiment_id
runner.py 从配置 seed 复制该任务文件
    ↓ 有 candidate_id 时
Docker 执行候选 Python 生成器，更新实验输出目录
    ↓
preflight.py 调用官方 metric，检查电路和数值文件
    ↓
Python 3.12 子进程运行对应的官方验证器
    ↓
store.py 保存状态、正确性报告、成本和文件哈希
    ↓ agent 用新的 Action ID 调用 GetExperiment 轮询
Outcome.data.text 返回 JSON → Runtime 回填结果 → agent 选择下一轮优化
```

省略 `candidate_id` 时直接验证配置的 seed，不运行候选生成器。候选 Python 只在禁网 Docker 容器中执行；官方预检和验证器在宿主机 Python 3.12 子进程执行，World 服务本身使用 Python 3.13。当前 Runtime 往返验收替换了网络 I/O，使用真实 Runtime、World 和 SDK Schema；生产 Hub 与付费模型尚未做端到端验收。

三项任务的实验独立运行，最终选取每项任务的一个完整验证结果打包：

```text
CreateCandidate → RunExperiment(mode="quick") → GetExperiment
    ↓ 通过后，使用同一候选运行 mode="full"
invsqrt full PASS + dqi full PASS + dfthcsos full PASS
    ↓ BuildSubmission 检查三个实验及文件哈希
计算本地估分，必须超过配置的 current_best
    ↓
生成 submission.zip 和 review.json，状态 awaiting_approval
    ↓ 由你人工审核报告与提交包，并确认完整 SHA-256
操作员执行 approve，审批绑定哈希，24 小时有效
    ↓ 操作员显式执行 submit
重查 Kaggle 全部提交历史和最高分；有待评分提交则停止
    ↓ 本地估分仍超过线上最高分，且审批、哈希均有效
消费单次审批 → 上传 Kaggle → 保存提交编号与回执
```

quick 仅作筛选，不能用于打包。合并本地估分是三个 `reference_cost / task_cost` 的几何平均；Kaggle 使用秘密 salt，本地估分不等于线上分数。agent 的六个工具是 `GetTask`、`CreateCandidate`、`RunExperiment`、`GetExperiment`、`CancelExperiment`、`BuildSubmission`，没有审批或上传工具。上传结果不明确时标记 `unknown`，必须先人工核对 Kaggle，不自动重试。

主要开发入口：

| 目的 | 入口 |
|---|---|
| 修改 agent 工具及业务流程 | [tools.py](quantum-world/src/quantum_world/tools.py)、[service.py](quantum-world/src/quantum_world/service.py) |
| 修改候选执行与官方验证调度 | [runner.py](quantum-world/src/quantum_world/runner.py) |
| 改进电路和数值优化算法 | [research](research/) |
| 对接独立 Agent Runtime | [公共协议、配置与联调](docs/agent-interface.md) |
| 安装、配置、启动与人工审批 | [Quantum World README](quantum-world/README.md) |

**线上结果：2026-11-06 11:00（北京时间）查询，提交 `58042263` 已 `COMPLETE`，公开分数 `1.00000`，无评分错误。** 机器可读回执见 `runs/submission-status.json`。

## 已固定的材料

- 比赛：`google-verifiable-quantum-circuit-compilation`
- 官方数据：`downloads/` 保存原始包，`official/data/` 保存解压内容。
- 官方评分 notebook：`official/metric/google-quantum-ai-metric.ipynb`。
- `official/metric/google_quantum_ai_metric.py` 只是将 notebook 代码单元串接导出，未修改评分逻辑。
- 本地 kickmix：Qualtran commit `9a0c11a4ee7697c082fbf8f2e1e8bf4a71fcb453`，macOS arm64 源码构建。
- 本机内存 16 GiB；此次 DQI 使用 2 个 worker，逆平方根使用 `--max-workers 4`。这些是本次成功运行的设置，不代表性能最优。
- 官方附带的 `kickmix.so` 是 Linux x86-64 二进制，未在本机使用。
- `runs/official-manifest.json` 和 `runs/environment.json` 保存材料哈希与构建来源。

## 本地验证结果

`runs/*-full.json` 为未修改官方验证器的输出，全部使用默认完整测试量和本地空 salt。

| 任务 | 状态 | shots | 本地任务成本（越低越好） |
|---|---|---:|---:|
| invsqrt | PASS | 200,540,928 | 2,076,774.233715282 |
| dqi | PASS，输出错误与相位错误均为 0 | 102,400 | 24,439,463,583,672.93 |
| dfthcsos | PASS | 254,464 | 3,355,599,588.3155365 |

本地完整测试不等同于 Kaggle 的秘密 salt 测试。线上结果以 `runs/submission-status.json` 为准。

## 复现命令

从本目录运行。凭据使用 Kaggle 标准配置或环境变量，脚本不包含 Token。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-local.lock.txt

# 预检官方电路与数值文件；不执行模拟
.venv/bin/python scripts/preflight.py

# 完整本地验证；按机器资源依次运行
.venv/bin/python scripts/verify_local.py invsqrt --workers 4
.venv/bin/python scripts/verify_local.py dqi --workers 2
.venv/bin/python scripts/verify_local.py dfthcsos

# 只打包官方基线，不改动电路内容
.venv/bin/python scripts/package_baseline.py

# 实际创建线上提交；已有提交编号时拒绝重复提交
.venv/bin/python scripts/kaggle_submission.py submit
.venv/bin/python scripts/kaggle_submission.py status
```

依赖锁是当前 macOS 环境的记录；Linux 服务器需重新构建扩展并验证兼容性，尚未测试。

## 提交约束与结果

- 上传文件必须准确命名为 `submission.zip`。首次尝试因文件名不同被 Kaggle 拒绝，原因保存在 `runs/submission-filename-rejection.json`。
- ZIP 内必须包含三个任务。此次根目录为 `invsqrt.kmb`、`dqi.kmb` 和 `dfthcsos/`，共 13 个文件。
- `runs/baseline-package.json` 保存逐文件哈希和提交包哈希。
- `runs/submission-receipt.json` 保存创建提交的回执；`runs/submission-status.json` 保存查询状态。
- 当前本机网络下整包上传曾停滞；提交脚本改用 256 KiB 分块传输，并设置网络超时。传输日志不包含上传 URL 或认证信息。
- 官方基线的预期榜单分约为 1.0。此次目标是验证提交流程，并非提升账号现有成绩。
- 本次实际提交编号：`58042263`，已评分完成，公开分数 `1.00000`。从创建到首次查询完成约 40 分钟。

## 当前本地环境与后续部署

当前本地 agent 接入使用上文的 Quantum World，具体启动命令见其 README。后续迁移服务器时，先重新构建该平台的 kickmix 与 Docker 镜像，复现三个基线成本，再接入 agent。保持官方材料只读，区分快速与完整验证，并保留上传前人工审批。不要将本地 salt 结果当作线上评分结果。
