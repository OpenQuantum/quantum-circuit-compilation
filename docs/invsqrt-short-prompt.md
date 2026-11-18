# invsqrt 短句优化的本机配置

在专用 `quantum-agent-local`、`quantum-world-invsqrt-local`、`local-user` 配对下，Runtime 用本机配置加载 [invsqrt 图谱优化流程](../skills/invsqrt-graph-optimization.md)。已有 Runtime 默认提示或自定义提示与其他配置保留。用户提供具体步骤时，Agent 沿用原有工具行为。

使用安装了对应 `agent_runtime` 的 Python，从独立 Console 工作树生成并检查配置。输出与原配置位于同一个忽略提交的状态目录，确保相对数据路径指向原位置。

```powershell
python scripts/prepare_invsqrt_runtime.py --base-config .local-console/live/runtime.toml --output .local-console/live/runtime.short-prompt.toml
python -m agent_runtime check-config --config .local-console/live/runtime.short-prompt.toml
```

World 确认旧实验已经结束后，保留其状态卷中的历史记录。把同一候选、同一电路文件的已通过 quick 和 full 设为基准，`baseline_parameters` 使用该生成器完整的五个整数字段。旧状态卷可只读挂载到复制进程，在新 World 使用复制后的卷。短句流程的 World 按优化轮次限制 3 次 quick 和 1 次 full。这次配置省略旧版通用 `[invsqrt_demo]` 配额。

```toml
[research]
url = "http://127.0.0.1:8787"

[invsqrt_optimization]
baseline_full_id = "exp-..."
baseline_quick_id = "exp-..."
baseline_parameters = { delta_bits = 13, coef_bits = 17, square_bits = 28, product_bits = 27, result_bits = 28 }
```

宿主启动 Workspace 时监听 `127.0.0.1:8787`，用 `QCC_AGENT_TOKEN` 接收研究读取，另用 `QCC_WORLD_RESULT_TOKEN` 接收实验结果写入和历史读取，`QCC_WORLD_RESULT_WORLD_ID` 填当前 World ID。World 分别用 `QUANTUM_RESEARCH_TOKEN`、`QUANTUM_RESEARCH_RESULTS_TOKEN` 配对应值。凭据只进入相关服务的环境。

Docker Desktop 上，在启动隔离 World 后，用 [loopback relay](../scripts/invsqrt_loopback_relay.py) 把容器内的 `127.0.0.1:8787` 指向宿主 Workspace。该转发只开放研究读取、结果历史读取和结果写入三个具体路由。每次重启容器后须重新启动它。

```powershell
$relay = Get-Content -LiteralPath scripts/invsqrt_loopback_relay.py -Raw
docker exec -d <演示World容器名> /opt/quantum-world/bin/python -c $relay
```

先用只读请求核对 Console ready、基准记录可读、研究和结果服务已正确鉴权。启动 Runtime 后检查其协商目录，再从网页发一次「请用知识图谱优化 invsqrt」。
