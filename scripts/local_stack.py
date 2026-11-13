"""Run the local console, external Runtime, authenticated Hub and PostgreSQL.

All generated configuration, credentials and logs stay in .local-console/live.
Run this supervisor in the foreground; SIGTERM stops its child services.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time
import tomllib
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local-console/live"
DB_NAME = "quantum-console-live-db"
spec = importlib.util.spec_from_file_location(
    "console_setup", ROOT / "scripts/console.py"
)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def write_once(path, text):
    if not path.exists():
        console.private_write(path, text)


def prepare(args):
    if not (STATE / "console.json").exists():
        console.initialize(
            STATE,
            hub_url="ws://127.0.0.1:28080",
            world_hub_url="ws://127.0.0.1:28080",
            current_best=25.08686,
            bff_port=8330,
            world_port=8331,
        )
        settings = f"""project_root = {json.dumps(str(args.data_project.resolve()))}
data_root = {json.dumps(str(STATE / "world"))}
worker_python = {json.dumps(str(args.data_project.resolve() / ".venv/bin/python"))}
seed_path = {json.dumps(str(args.data_project.resolve() / "research/candidates/rotated50"))}
current_best = 25.08686
[hub]
url = "ws://127.0.0.1:28080"
world_id = "quantum-world-local"
[hub.allowed_agents]
quantum-agent-local = ["local-user"]
"""
        (STATE / "world.toml").write_text(settings)
    for name in (
        "hub-client-token",
        "hub-admin-token",
        "postgres-password",
        "research-token",
        "research-download-token",
    ):
        write_once(STATE / name, secrets.token_urlsafe(48) + "\n")
    world_config = STATE / "world.toml"
    if "[research]" not in world_config.read_text():
        with world_config.open("a") as stream:
            stream.write('\n[research]\nurl = "http://127.0.0.1:8787"\n')
    # Update only the research tools in this launcher's private directory.
    directory = STATE / "worlds.json"
    worlds = json.loads(directory.read_text())
    for world in worlds:
        world["allowedTools"] = sorted(
            set(world["allowedTools"])
            | {
                "SearchResearchMethods",
                "GetResearchNode",
                "SearchResearchSolutions",
                "StartResearchSourceDownload",
                "GetResearchDownloadJob",
                "GetResearchSnapshotStructure",
                "ReadResearchSnapshotSource",
                "ImportResearchSnapshot",
            }
        )
    directory.write_text(json.dumps(worlds, ensure_ascii=False, indent=2))
    if not (args.research_repo / ".runtime/workspace.sqlite").exists():
        subprocess.run(
            ["node", str(args.research_repo / "src/setup.mjs")],
            cwd=args.research_repo,
            check=True,
        )
    source = tomllib.loads(args.model_config.read_text())["generator"]
    if not source.get("api_key") or not source.get("model_id"):
        raise ValueError(
            "Existing model config must contain generator.model_id and api_key"
        )
    # Re-read credentials at each start; no credential is put into logs or TOML.
    key_path = STATE / "model-key"
    if not key_path.exists():
        console.private_write(key_path, source["api_key"])
    else:
        key_path.write_text(source["api_key"])
        key_path.chmod(0o600)
    runtime_config = f"""[runtime]
instance_id = "quantum-agent-local"
storage_path = {json.dumps(str(STATE / "runtime/core.sqlite3"))}
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
storage_path = {json.dumps(str(STATE / "runtime/human.sqlite3"))}
[generator]
model_id = {json.dumps(source["model_id"])}
max_rounds = 16
request_timeout = 90.0
call_timeout = 120.0
[world_tools]
enabled = true
[conversation]
decision_enabled = false
voice_enabled = false
"""
    path = STATE / "runtime.toml"
    path.write_text(runtime_config)
    path.chmod(0o600)
    return source["model_id"]


def source_python(research_repo, explicit):
    candidate = explicit or research_repo / ".runtime/kaggle-venv/bin/python"
    candidate = Path(os.path.abspath(Path(candidate).expanduser()))
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    if explicit:
        raise ValueError("The configured research source Python must be executable")
    return None


def research_proxy_env(environ):
    values = {
        name: environ[name]
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
        if environ.get(name)
    }
    exclusions = ["127.0.0.1", "localhost", "::1"]
    for name in ("NO_PROXY", "no_proxy"):
        for host in environ.get(name, "").split(","):
            host = host.strip()
            if host and host not in exclusions:
                exclusions.append(host)
    values["NO_PROXY"] = values["no_proxy"] = ",".join(exclusions)
    return values


def main(args):
    research_source_python = source_python(args.research_repo, args.research_source_python)
    model = prepare(args)
    client = (STATE / "hub-client-token").read_text().strip()
    password = (STATE / "postgres-password").read_text().strip()
    base = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "HOME", "LANG", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
    }
    base.update(
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        NO_PROXY="127.0.0.1,localhost,::1",
        HUB_CLIENT_TOKEN=client,
    )
    bff_python = str(ROOT / "quantum-bff/.venv/bin/python")
    runtime_python = str(args.runtime_repo / ".venv/bin/python")
    children = []
    logs = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def spawn(name, command, env):
        log = (STATE / (name + ".log")).open("ab")
        logs.append(log)
        child = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
        )
        children.append((name, child))
        (STATE / "processes.json").write_text(
            json.dumps({n: p.pid for n, p in children})
        )

    def wait_ready(url, headers=None):
        for _ in range(120):
            for name, child in children:
                if child.poll() is not None:
                    raise RuntimeError(
                        f"{name} exited; inspect {STATE / (name + '.log')}"
                    )
            try:
                with urlopen(
                    Request(url, headers=headers or {}), timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"Service not ready: {url}")

    try:
        probe = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "quantum.console.managed"}}',
                DB_NAME,
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode:
            env = dict(base, POSTGRES_PASSWORD=password)
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    DB_NAME,
                    "--label",
                    "quantum.console.managed=local-live",
                    "--restart",
                    "unless-stopped",
                    "-p",
                    "127.0.0.1:54329:5432",
                    "-v",
                    "quantum-console-live-db-data:/var/lib/postgresql/data",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_USER=quantum",
                    "-e",
                    "POSTGRES_DB=quantum",
                    "postgres:17-alpine",
                ],
                env=env,
            )
        elif probe.stdout.strip() != "local-live":
            raise RuntimeError("Database container name belongs to another deployment")
        else:
            subprocess.check_output(["docker", "start", DB_NAME])
        for _ in range(120):
            if (
                subprocess.run(
                    ["docker", "exec", DB_NAME, "pg_isready", "-U", "quantum"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.25)
        else:
            raise TimeoutError("PostgreSQL is not ready")
        spawn(
            "hub",
            [bff_python, "-m", "hub_server", "--host", "127.0.0.1", "--port", "28080"],
            dict(
                base,
                PYTHONPATH=str(args.hub_repo),
                HUB_ADMIN_TOKEN=(STATE / "hub-admin-token").read_text().strip(),
                HUB_DB_PATH=str(STATE / "hub.sqlite3"),
            ),
        )
        wait_ready("http://127.0.0.1:28080/health")
        research_token = (STATE / "research-token").read_text().strip()
        research_download_token = (STATE / "research-download-token").read_text().strip()
        research_env = dict(
            base,
            QCC_HOST="127.0.0.1",
            QCC_PORT="8787",
            QCC_AGENT_TOKEN=research_token,
            QCC_AGENT_DOWNLOAD_TOKEN=research_download_token,
            QCC_DATA_DIR=str(args.research_repo / ".runtime"),
        )
        research_env.update(research_proxy_env(os.environ))
        if research_source_python:
            research_env["QCC_SOURCE_PYTHON"] = research_source_python
        spawn(
            "research",
            ["node", str(args.research_repo / "src/server.mjs")],
            research_env,
        )
        wait_ready(
            "http://127.0.0.1:8787/api/agent/research?task=invsqrt&limit=1",
            {"Authorization": "Bearer " + research_token},
        )
        spawn(
            "world",
            [
                bff_python,
                "-m",
                "quantum_world.cli",
                "--config",
                str(STATE / "world.toml"),
                "serve",
                "--operator-http",
                "--http-port",
                "8331",
            ],
            dict(
                base,
                QUANTUM_OPERATOR_TOKEN=(STATE / "operator-token").read_text().strip(),
                QUANTUM_RESEARCH_TOKEN=research_token,
                QUANTUM_RESEARCH_DOWNLOAD_TOKEN=research_download_token,
            ),
        )
        wait_ready(
            "http://127.0.0.1:8331/operator/v1/summary",
            {
                "Authorization": "Bearer "
                + (STATE / "operator-token").read_text().strip(),
                "X-Quantum-User": "local-user",
            },
        )
        runtime_env = dict(
            base,
            RUNTIME_MODEL_API_KEY_FILE=str(STATE / "model-key"),
            AGENT_RUNTIME_POSTGRES_DSN=f"postgresql://quantum:{password}@127.0.0.1:54329/quantum",
            AGENT_RUNTIME_POSTGRES_ROOT=str(STATE),
            AGENT_RUNTIME_POSTGRES_NAMESPACE="quantum_live",
            HTTPS_PROXY="http://127.0.0.1:10080",
            HTTP_PROXY="http://127.0.0.1:10080",
        )
        spawn(
            "runtime",
            [
                runtime_python,
                "-m",
                "agent_runtime",
                "serve",
                "--config",
                str(STATE / "runtime.toml"),
            ],
            runtime_env,
        )
        spawn(
            "bff",
            [
                bff_python,
                str(ROOT / "scripts/console.py"),
                "--state-dir",
                str(STATE),
                "serve-bff",
            ],
            base,
        )
        wait_ready("http://127.0.0.1:8330/")
        print(
            json.dumps(
                {
                    "status": "ready",
                    "url": "http://127.0.0.1:8330/",
                    "model": model,
                    "login_user": "local-user",
                    "login_code_file": str(STATE / "login-code"),
                }
            ),
            flush=True,
        )
        while True:
            for name, child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"{name} stopped; inspect its log")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, child in reversed(children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for _, child in reversed(children):
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-repo", type=Path, default=ROOT.parent / "star-agent-runtime"
    )
    parser.add_argument(
        "--data-project", type=Path, default=ROOT.parent / "quantum-search"
    )
    parser.add_argument("--hub-repo", type=Path, required=True)
    parser.add_argument(
        "--research-repo", type=Path, default=ROOT.parent / "quantum-research-workspace"
    )
    parser.add_argument("--research-source-python", type=Path)
    parser.add_argument("--model-config", type=Path, required=True)
    main(parser.parse_args())
