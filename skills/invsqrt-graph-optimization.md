# invsqrt 图谱优化

仅当最新用户消息简短地提出「请用知识图谱优化 invsqrt」，且没有指定基准、参数、工具或验证步骤时，执行以下流程。用户给出详细指令时，按这些指令和现有工具契约执行。

1. 调用 `GetTask({"task":"invsqrt"})`。读取工具返回的 `baseline.full_id`、`baseline.parameters` 和 `history`。使用当前已完整验证的基准（本次初始基准为 H14），并记录历史已试位宽。缺少基准或历史，或收到 `optimization_error`、`history_error` 时，向用户报告具体缺项。
2. 调用 `SearchResearchMethods({"task":"invsqrt","query":"precision","limit":2,"fallback":true})`。从真实搜索结果选择与位宽或精度相关的节点，再以返回的节点 ID 调用 `GetResearchNode({"task":"invsqrt","node_id":"<实际返回的节点 ID>"})`。只用实际读到的节点提出方案；工具失败或没有可用节点时报告具体原因。
3. 从 `baseline.parameters` 出发，提出 1–3 组小幅位宽变化。每组关联一个已读的 `method_node_id`，用 `changes` 只填改动的整数字段。每个变动字段只调整 1–2 位，变动后距离官方默认值最多 4 位，优先单字段小幅调整。排除与当前基准相同、与 `history` 已试组合相同、以及提案内重复的组合。这个短句流程直接使用基准参数和方法节点，不分段读取参考源码。
4. 调用一次 `StartInversqrtOptimization({"baseline_full_id":"<baseline.full_id>","proposals":[{"method_node_id":"<已读节点 ID>","changes":{"<位宽字段>":<整数>}}]})`。由 World 执行候选筛选和完整验证。提交后直接按回执回复启动状态、任务编号或错误；不调用 `action_wait`，不循环查实验，也不把排队或启动说成已通过或已提分。
