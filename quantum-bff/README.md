# Quantum Competition BFF

比赛专用 BFF，与 `../frontend/` 一起维护在本仓库。通用认证、聊天、HTTP/WebSocket、Run 持久化和重连使用固定版本的 `star-bff-for-human`；本目录仅增加比赛 API 和静态前端入口。没有复制或修改 Base World、Agent Runtime 源码。

完整安装、启动、身份对接及人工审批说明见 [本地比赛控制台](../docs/console.md)。

```text
src/quantum_bff/
├── main.py       # 组合 Star BFF、比赛路由、前端和生命周期
├── config.py     # World 服务地址、服务凭据和前端目录
├── client.py     # 受信 HTTP 调用、错误映射、大文件流式代理
├── api.py        # 登录/CSRF 保护的比赛端点
└── cli.py        # quantum-bff，默认 127.0.0.1:8320
```

BFF 以登录会话的用户身份调用 Quantum World 的操作者 API，不读取 World SQLite、不伪造 Agent sender。评分、实验状态、审批、Kaggle 上传都由 World 决定。普通 Run 的 `approve/reject` 与 Kaggle 包审批是两种不同操作。

`pyproject.toml` 固定通用 BFF 为 `16494e09dab558ae4f4e2acb6ca2011d13e8f1b8`，公共 Star SDK 为 `c237172f788412a93a3647129572b546ca949603`。使用 Python 3.13+：

```sh
uv sync --locked --extra test
.venv/bin/python -m pytest -q
```

若希望安装已经克隆的通用 BFF 来做本地接口开发，可在上述安装后显式执行 `uv pip install --python .venv/bin/python --no-deps -e /path/to/star-agent-bff`；这只改变本虚拟环境，不修改上游源码。此时运行的是本地 checkout，应另行记录其提交，不应把它当作锁文件版本的验收结果。
