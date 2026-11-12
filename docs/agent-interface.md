# 外部 Agent 接口

Agent Runtime 独立维护于 [OpenQuantum/star-agent-runtime](https://github.com/OpenQuantum/star-agent-runtime)。本项目只提供 Quantum World、比赛材料和优化算法，不包含或导入 Runtime 源码。双方通过独立 Star Hub 的 WebSocket/Star Protocol 通信；没有额外的 REST 接口或共享数据库要求。

```text
外部 Runtime 仓库与进程 → Star Hub ← 本项目 Quantum World 进程
                           │
                 能力协商、Action、Outcome
```

## 协议与身份

- 协议：`agent-env-compact/2`。World 的公共 `star-protocol` SDK 固定在 star-hub 提交 `c237172f788412a93a3647129572b546ca949603`；Runtime 独立选择兼容的版本，不要求共享 Python 环境。
- 双方配置同一个 Hub URL；World 以 `world_id` 注册 Environment，Runtime 以自己的 `agent_id` 加入该 Environment。
- Hub token 通过 `Authorization: Bearer …` 握手，使用环境变量或终端隐藏输入，不放进 Action。
- World 的 `[hub.allowed_agents]` 将已认证的 `agent_id` 映射到允许的 `userId`。Runtime 必须从可信用户上下文补入 `userId`；它不是模型自由生成的身份。候选和实验按 `(sender, userId)` 隔离。
- 双方独立启动、部署和更新。跨机器连接时使用可达的 Hub 地址和适当的 `wss://` 配置。

## 公开工具

实际参数 Schema 从 World 能力协商获取，源码定义见 [tools.py](../quantum-world/src/quantum_world/tools.py)。各工具均要求 `userId`。

| 工具 | 其他必填参数 | 可选参数 |
|---|---|---|
| GetTask | task | — |
| SearchResearchMethods | task | query、limit |
| GetResearchNode | task、node_id | — |
| CreateCandidate | task、files、entrypoint | description |
| RunExperiment | task、mode | candidate_id |
| GetExperiment | experiment_id | — |
| CancelExperiment | experiment_id | — |
| BuildSubmission | experiments（三个完整实验 ID） | — |

`task` 是 `invsqrt`、`dqi`、`dfthcsos`；`mode` 是 `quick` 或 `full`。候选使用受限相对路径到 UTF-8 源码的映射，输出文件通过 World 管理，不传任意宿主机路径。

公开研究图谱工具的服务配置与数据范围见[研究工作台接入](research-workspace-link.md)。

## 请求与回执

以下是 SDK 封装的内容对象，不是完整 WebSocket Envelope。请使用 SDK 处理路由、协商和关联。

```json
{"id":"action-unique-id","name":"GetTask","params":{"userId":"local-user","task":"invsqrt"}}
```

```json
{"id":"outcome-unique-id","refs":["action-unique-id"],"status":"completed","data":{"text":"{\"task\":\"invsqrt\",\"approval_required\":true}"}}
```

回执内容示例省略了其他业务字段。SDK 要求结果位于 `data.text` 中，调用方需再解析一次 JSON。失败回执使用 `rejected`，文本中是 `{"error":{"code":"…","message":"…"}}`。相同身份下同一 Action ID 重发会重放原始终态；改变参数必须使用新 ID，否则触发幂等冲突。

`RunExperiment` 立即返回 `experiment_id`；工具回执 `completed` 不表示实验成功。用新 Action ID 调用 `GetExperiment` 轮询；实验可处于 `queued`、`running`、`succeeded`、`failed`、`cancelled`、`interrupted`。取消回执可能是 `cancelling`，应继续查询直到工作停止。判断通过需检查实验结果，而非只检查请求状态。

quick 不能打包。三个任务完整验证通过且本地估分超过配置门槛，`BuildSubmission` 才返回 `awaiting_approval` 提交包。Agent 接口没有 approve/submit；Kaggle 上传始终需要独立的人工审批，原有哈希绑定和单次审批限制继续生效。

## 两个仓库的配置

本项目配置样例见 [config.example.toml](../quantum-world/config.example.toml)。在外部 Runtime 仓库自己的配置中合并：

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

保留外部仓库已有的模型和 Human 配置，使用实际身份替换示例。先启动 Hub，再在本项目启动 World，最后从外部仓库启动 Runtime。Runtime 启动方式以其独立仓库说明为准。

## 独立接口联调

从本项目 `quantum-world` 目录执行，无需安装 Runtime：

```bash
.venv/bin/python examples/agent_client.py --prompt-token
```

此示例仅依赖公共 SDK，执行加入、能力发现和 `GetTask` 调用；需要实际可用的 Hub 与 World，不调用模型或 Kaggle。接口离线测试为：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_protocol_contract.py` 验证公共 SDK Schema、工具目录、回执重放、身份拒绝和异步实验查询，不导入 Runtime 内部类。它不替代真实 Hub 的网络端到端验收。此前 Runtime 源码审计保留为[历史记录](runtime-integration-audit.json)，不构成当前项目依赖。
