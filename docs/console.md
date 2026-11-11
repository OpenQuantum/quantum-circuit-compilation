# 从源码启动比赛控制台

控制台在同一个本机地址提供 Agent 对话、实验列表和验证日志。浏览器通过 BFF 与已有 Agent Runtime 对话。Agent Runtime 经 Star Hub 调用 Quantum World。BFF 通过 World 的 `/operator/v1` 接口读取实验和日志。

## 准备现有服务与官方数据

- 使用 Git、uv、Python 3.13 和 Docker Desktop 的 Linux 容器。Windows 使用下方 PowerShell 命令。macOS 可使用后面的 shell 命令。
- 准备已有 Star Hub 与 Agent Runtime 的配置和凭据。Windows 验收复用了本地 Hub 和 Agent Runtime `53013476667e0627ba99bba772e2ee84aa771486`，模型为 `deepseek/deepseek-flash`。接口字段见[外部 Agent 接口](agent-interface.md)。
- 安装 BFF 需要读取 `quantum-bff/uv.lock` 中固定提交的团队 Git 仓库。准备已有 Star Hub 源码以导出固定版本的 Star Protocol SDK。
- 下载 [Kaggle 比赛数据](https://www.kaggle.com/competitions/google-verifiable-quantum-circuit-compilation/data)，将解压后的 `verifiers/` 和 `sample_submission/` 等目录放在同一个官方数据目录。文件路径与 SHA256 见 [official-manifest.json](../runs/official-manifest.json)。World 运行时只读挂载该目录和本仓库的 `official/metric/`。

## 初始化与构建

从仓库根目录运行下列 PowerShell 命令。示例中 Hub 从宿主机访问时用 `ws://127.0.0.1:28080`，在已有 Docker 网络内用 `ws://hub:8000`。按实际部署替换这两个地址和 Docker 网络名。

```powershell
uv sync --project quantum-bff --locked --python 3.13
uv run --project quantum-bff --no-sync python scripts/console.py --state-dir .local-console init --hub-url ws://127.0.0.1:28080 --world-hub-url ws://hub:8000

$hubRepo = '..\star-hub'
$sdkRevision = 'c237172f788412a93a3647129572b546ca949603'
$state = (Resolve-Path -LiteralPath '.local-console').Path
git -C $hubRepo archive --format=zip --output="$state/star-sdk.zip" $sdkRevision vendor/star-protocol
Expand-Archive -LiteralPath "$state/star-sdk.zip" -DestinationPath "$state/sdk"
docker build --build-context "star_sdk=$state/sdk/vendor/star-protocol" -f quantum-world/validation/Dockerfile -t quantum-world-console:local quantum-world
```

初始化生成 `world.toml`、登录账号和 BFF 服务目录。默认身份为 `local-user`、`quantum-agent-local`、`quantum-world-local` 和 `human-bff.quantum.local`。可用 `init --help` 查看自定义参数。登录口令和 World HTTP 服务凭据保存在 `.local-console`。

macOS 上使用同一条 `uv sync` 和 `init` 命令。SDK 的导出和构建命令如下，`hub_repo` 改为本机已有 Hub 源码位置。

```bash
hub_repo=../star-hub
sdk_revision=c237172f788412a93a3647129572b546ca949603
state="$PWD/.local-console"
git -C "$hub_repo" archive --format=zip --output="$state/star-sdk.zip" "$sdk_revision" vendor/star-protocol
mkdir -p "$state/sdk"
unzip "$state/star-sdk.zip" -d "$state/sdk"
docker build --build-context "star_sdk=$state/sdk/vendor/star-protocol" -f quantum-world/validation/Dockerfile -t quantum-world-console:local quantum-world
```

## 启动 World 与网页

先启动已有 Hub。将该 Hub 的客户端凭据设置为当前终端的 `HUB_CLIENT_TOKEN`。World、Agent Runtime 和 BFF 都从进程环境取凭据。macOS 终端需执行 `export HUB_CLIENT_TOKEN`，子进程才能读取该变量。选择一个未被使用的 World ID，或先停掉占用同一 ID 的 World。示例中的 `ws://hub:8000` 要求 World 与 Hub 在同一 Docker 网络，且 Hub 在该网络上的别名为 `hub`。

Windows PowerShell 示例。将 `$officialData` 和 `$hubNetwork` 替换成实际路径和网络名。

```powershell
$officialData = (Resolve-Path -LiteralPath 'E:\path\to\official\data').Path
$officialMetric = (Resolve-Path -LiteralPath 'official/metric').Path
$hubNetwork = 'your-existing-hub-network'
$env:QUANTUM_OPERATOR_TOKEN = [IO.File]::ReadAllText("$state/operator-token").Trim()
docker volume create quantum-world-console-state
docker run -d --name quantum-world-console --network $hubNetwork `
  --publish 127.0.0.1:8321:8321 `
  --mount "type=bind,source=$state/world.toml,target=/config/world.toml,readonly" `
  --mount "type=bind,source=$officialData,target=/project/official/data,readonly" `
  --mount "type=bind,source=$officialMetric,target=/project/official/metric,readonly" `
  --mount 'type=volume,source=quantum-world-console-state,target=/state' `
  --env HUB_CLIENT_TOKEN --env QUANTUM_OPERATOR_TOKEN quantum-world-console:local
```

macOS 对应命令如下。保持在仓库根目录，并沿用上节的 `state`。

```bash
official_data=/path/to/official/data
hub_network=your-existing-hub-network
IFS= read -r QUANTUM_OPERATOR_TOKEN < "$state/operator-token"
export QUANTUM_OPERATOR_TOKEN
docker volume create quantum-world-console-state
docker run -d --name quantum-world-console --network "$hub_network" \
  --publish 127.0.0.1:8321:8321 \
  --mount "type=bind,source=$state/world.toml,target=/config/world.toml,readonly" \
  --mount "type=bind,source=$official_data,target=/project/official/data,readonly" \
  --mount "type=bind,source=$PWD/official/metric,target=/project/official/metric,readonly" \
  --mount 'type=volume,source=quantum-world-console-state,target=/state' \
  --env HUB_CLIENT_TOKEN --env QUANTUM_OPERATOR_TOKEN quantum-world-console:local
```

在已有 Agent Runtime 的本机配置里合并下面的配置。保留当前模型、凭据与存储配置。自定义了控制台身份或 Hub 地址时，同步修改对应字段。

```toml
[hub]
url = "ws://127.0.0.1:28080"
agent_id = "quantum-agent-local"
world_id = "quantum-world-local"

[human]
enabled = true
human_client_id = "human-bff.quantum.local"
allowed_user_id = "local-user"
dialogue_mode = "standard"
execution_backend = "generator"

[world_tools]
enabled = true
```

在已安装依赖的 Runtime 仓库根目录运行 `uv run --no-sync python -m agent_runtime serve --config /path/to/runtime.toml`，配置路径替换为本机路径。确认 Runtime 加入 World 后，在本仓库根目录启动 BFF。

```text
uv run --project quantum-bff --no-sync python scripts/console.py --state-dir .local-console serve-bff
```

打开 `http://127.0.0.1:8320/`，用 `local-user` 和 `.local-console/login-code` 中的口令登录。比赛面板以只读方式启动。实验状态和日志从 World 读取。

## 从空实验库验收

本页的镜像和启动步骤已在 Windows + Docker Desktop 上完成 `invsqrt` quick 验收。Apple Silicon 可用 Docker Desktop 默认的 ARM64 构建，验收以本机运行结果为准。

1. 从网页请 Agent 调用 `GetTask(task="invsqrt")`。
2. 从网页请 Agent 调用一次 `RunExperiment(task="invsqrt", mode="quick")`，省略 `candidate_id`，使用官方样例。记下回执里的实验 ID。
3. 等实验列表显示「验证成功」，从网页请 Agent 用同一 ID 调用 `GetExperiment`。打开该实验的 `verify.stdout`，核对 `PASS`、`262144` shots 和诊断成本。

自动验收使用 Node.js、Playwright 和本机 Chrome。将 Playwright 安装在本地状态目录，前端由 BFF 直接提供。

```powershell
npm install --prefix .local-console/test-tools --no-save playwright@1.64.1
$playwright = (Resolve-Path -LiteralPath '.local-console/test-tools/node_modules/playwright').Path
node scripts/tests/console_live_e2e.mjs --playwright $playwright
```

macOS 使用同一条 npm 安装命令，再以绝对路径传入 Playwright 模块。

```bash
node scripts/tests/console_live_e2e.mjs --playwright "$PWD/.local-console/test-tools/node_modules/playwright"
```

重跑同一验收时使用同一个状态文件。脚本会接续已保存的 quick 请求。为一轮全新验收指定新的 `--state-file`，并使用新的 World 数据卷。

## 运行回归测试

前端运行 `npm --prefix frontend test`。启动配置运行 `python -m unittest discover -s scripts/tests -v`。BFF 测试会加载真实 World 实例，需在 Linux 容器或其他提供 `fcntl` 的环境中运行。

```sh
uv run --project quantum-bff --locked --extra test python -m pytest -q quantum-bff/tests
```
