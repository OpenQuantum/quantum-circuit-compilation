# 在容器化 World 中运行候选生成器

`RunExperiment` 带 `candidate_id` 时，World 会启动候选生成器容器，然后使用独立的 Python 3.12 worker 验证输出。省略 `candidate_id` 时，World 直接验证 seed。

先按[控制台说明](console.md)准备 Star SDK 构建上下文。使用支持 `volume-subpath` 的 Docker Engine，从仓库根目录构建镜像。以下命令可在 Windows PowerShell 或 macOS shell 执行，`state` 沿用控制台说明中的本地状态目录。

```text
docker build -t quantum-world-generator:local quantum-world
docker build --build-context "star_sdk=$state/sdk/vendor/star-protocol" -f quantum-world/validation/Dockerfile -t quantum-world-invsqrt:local quantum-world
```

可信 World 持有 Docker socket。该 socket 可以管理 Docker daemon，部署时只把它提供给受信任的 World 镜像。使用同一个本机 Linux daemon，保留 World 的 Docker 默认 hostname，并为它指定显式容器名称。启动配置使用 `project_root = "/project"` 和 `data_root = "/state/world"`。

给 World 增加以下选项。先把 `verifiers/` 和 `sample_submission/invsqrt.kmb` 复制到专用 `quantum-world-invsqrt-official` 命名卷的根目录，再按下列方式只读挂载。`official/metric` 和 `world.toml` 仍按控制台说明分别只读挂载，Web 和 Hub 的参数继续沿用原配置。

```text
--name quantum-world-invsqrt-local
--env QUANTUM_DOCKER_SELF=quantum-world-invsqrt-local
--mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock
--mount type=volume,source=quantum-world-invsqrt-official,target=/project/official/data,readonly,volume-nocopy
--mount type=volume,source=quantum-world-invsqrt-state,target=/state,volume-nocopy
```

World 按运行时实际挂载解析候选目录、官方数据和本实验输出。候选只获得以下三个挂载，不继承 World 的 Docker socket、凭据或其他实验数据。

| 候选内路径 | 来源 | 权限 |
| --- | --- | --- |
| `/candidate` | 状态卷的 `world/candidates/<candidate_id>` | 只读 |
| `/official` | 官方数据命名卷根目录 | 只读 |
| `/out` | 状态卷的 `world/experiments/<experiment_id>/input` | 读写 |

候选使用 `--network none`、只读根文件系统、移除所有 capabilities，并限制 CPU、内存、进程数和 `/tmp`。World 以 root 运行时，候选使用 `65534:65534`，仅将该实验的输出目录及预置 seed 文件交给这个 UID。候选镜像必须预先构建，运行时使用 `--pull=never`。原生 Linux 宿主上的 World 保留既有 bind mount 模式。

用 `examples/identity.py` 的内容创建 `invsqrt` 候选，带该 `candidate_id` 发起 `mode="quick"` 实验。验收时核对 `generator.stdout` 中的 `Round-trip compiled: invsqrt.kmb`、`GetExperiment.status = succeeded`、官方 `PASS` 和 262144 shots。身份生成器读写同一个电路，这一步验证 Docker 执行和官方判分通道。正式优化结果须另与同等条件的官方基线比较。

## 验收 Agent 自主优化

给这一轮创建独立的状态卷和 World ID。将 `seed_path` 指向 `/project/official/data/sample_submission`，使用真实官方 `invsqrt.kmb`。其 SHA-256 为 `e2bf8eb64350a4460a271fc7f1d591ef125b1e276377e9a0f64b96ab3ca321e1`。配置无需 `[research]` 段。

先用同一 World 和同一 Agent、用户身份登记官方样例的 `full` 实验，省略 `candidate_id`，等待验证通过。可另跑一条样例 `quick` 供筛选对照。以操作员身份通过独立 SDK 调用时，等基线回执保存后再启动使用同一 Agent ID 的 Runtime，避免同一身份同时连接两次。

在已有 Hub、Runtime 和 BFF 连通后，从仓库根目录运行下面的网页验收脚本。端口和登录口令路径按实际启动参数替换，`--playwright` 使用已安装模块的绝对路径。按需加 `--baseline-quick-experiment-id` 提供 quick 对照。

```text
node scripts/tests/invsqrt_optimization_live_e2e.mjs --base-url http://127.0.0.1:8330 --world quantum-world-invsqrt-local --login-code-file .local-console/invsqrt/login-code --state-file .local-console/invsqrt/optimization-evidence.json --baseline-experiment-id <full-experiment-id> --playwright <absolute-playwright-module-path>
```

脚本从网页发一次优化目标，预算为最多 3 次 quick 和 1 次候选 full，随后读取 BFF 的工具记录和 World 的官方验证日志。复查同一次运行时使用原状态文件。若 Agent 结束时 quick 仍在运行，脚本在确认原实验完成后可以用 `--continue-pending` 在原对话续接。

`ReadInversqrtReference` 可以按行读取固定的官方参考生成器源码。它校验源码 SHA-256，供 Agent 在创建候选前查看。

另开一轮预算时，可以用新的 `--state-file` 和一个或多个 `--prior-attempt-experiment-id`，把之前已结束的候选 quick 记录交给 Agent 自己参考。

验收记录包含输入、Run、Action/Outcome、候选、实验、构建产物与验证器哈希，以及完整验证成本。退出码 `0` 表示验证确认成本下降，`2` 表示两份完整验证可比且未下降，其他非零状态请查看报告。这里比较的是 `invsqrt` 单项本地成本。
