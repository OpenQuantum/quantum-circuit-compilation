# Agent 连接研究图谱

本机研究工具仓库为 `../quantum-research-workspace`，下载版本 `447fac3`。它原本提供 Node.js 图谱网页和 HTTP API，不直接实现 Star Protocol。现在通过本仓库的 World 适配器把公开方法作为 agent 工具提供，Runtime 保持独立、无需改源码。

```text
前端 :8330 → BFF → Runtime → Hub → Quantum World
                                      ↓ 只读 HTTP + 专用服务凭据
                              research-workspace :8787
```

## 可调用工具

| 工具 | 参数 | 返回 |
|---|---|---|
| SearchResearchMethods | task，query 可选，limit 1–8 | 公开方法摘要、来源与版本、公开报告分数、节点 ID |
| GetResearchNode | task、node_id | 公开原理说明、来源、证据和公开父节点 |

task 沿用 `invsqrt`、`dqi`、`dfthcsos`；服务端将 `dfthcsos` 映射为图谱的 `chemistry`。默认优先展示已有公开分项倍率较高的节点；未报告分数不编造为 0。检索不代表图谱中每个数值已被本地官方验证。

部分公开版本节点只有标题和摘要，没有长篇原理说明，详情中的 explanation 将保持为空，不能据此补造内容。需要详解时可按方法关键字检索已有整理节点。

两个工具都保留 Runtime 的可信 userId 注入、World 身份校验、Action 幂等记录和 Outcome.data.text JSON 约定。图谱正文只作为外部参考数据，不能授予代码执行权限或替代用户指令。它们不启动采集、不发布节点、不上传成绩，不修改 Kaggle 提交的人工审批要求。

## 接口和配置

研究仓库新增 `GET /api/agent/research?task=invsqrt&query=&limit=2`，详情通过 `node_id` 查询。接口只接收 GET，要求 `Authorization: Bearer <QCC_AGENT_TOKEN>`，只返回 source.type 为 public/official 的节点及明确投影字段，不包含成员投稿、投稿人、同学复现报告或社区成绩。未配置服务凭据时拒绝访问。

World 的配置为：

```toml
[research]
url = "http://127.0.0.1:8787"
```

World 通过 `QUANTUM_RESEARCH_TOKEN` 接收同一个服务凭据。该初版适配器只接受明确的本机 HTTP origin，禁止重定向，不允许 agent 提供任意 URL。超时为 8 秒，响应大小和协议文本预算受限。较长说明被截断时返回 explanation_truncated。

`scripts/local_stack.py` 已负责生成专用凭据、初始化公开快照、启动研究服务、给 World 注入凭据并更新本地 BFF 工具目录。重新启动整条链路沿用[本地联调说明](local-runtime-link.md)，可用 `--research-repo` 指定外部工作台位置。运行数据仍分别保存在工作台 `.runtime/` 与比赛 `.local-console/live/`，都不提交 Git。

## 代码与验证

比赛仓库：`quantum_world/research.py` 是受限 HTTP 客户端，tools.py 声明工具，service.py 调用并保存回执。HTTP I/O 在 SQLite 事务外执行，重复 Action 重放原结果。

研究仓库：`src/agent-read.mjs` 负责公开数据筛选和输出预算，server.mjs 提供服务身份保护的接口。没有直接复用会混入成员数据的完整 graph 响应。

```bash
# 比赛仓库
quantum-bff/.venv/bin/python -m pytest --import-mode=importlib \
  quantum-world/tests/test_research.py quantum-world/tests/test_protocol_contract.py \
  quantum-world/tests/test_operator_api.py scripts/tests quantum-bff/tests -q
# 研究仓库
npm test
# 已启动全链路后，在比赛仓库运行真实浏览器 + 模型验收
node scripts/tests/research_live_e2e.mjs
```

浏览器验收状态保存在 `.local-console/live/research-browser-evidence.json`，复跑优先检查已保存的 runId，不重复发起模型请求。只验证检索和详情，不运行实验或发布研究结果。

2026-11-12 真实网页验收通过，runId 为 `run_a872f7f3971e4ef1b799bedd7bc0dd40`，先调用 SearchResearchMethods，再调用 GetResearchNode，读取公开节点 `invsqrt-hw-25` 的摘要和 Kaggle 数据集来源。研究仓库 43 项测试、比赛端完整回归 59 项测试（另含 14 个子测试）及前端 9 项测试通过。两仓库分别维护接口与适配器源码；服务凭据、运行数据库和本机浏览器验收记录不纳入 Git。
