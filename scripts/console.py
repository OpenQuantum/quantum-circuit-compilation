#!/usr/bin/env python3
"""Prepare local console configuration and run the host BFF."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"[a-zA-Z0-9._:-]{1,128}\Z")
TOOLS = ["GetTask", "SearchResearchMethods", "GetResearchNode", "CreateCandidate", "RunExperiment", "GetExperiment", "CancelExperiment", "BuildSubmission"]
HOST_ENVIRONMENT = {
    "PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
    "TEMP", "TMP", "TMPDIR", "LANG", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
}


def private_write(path, text):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)


def hub_endpoint(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"ws", "wss"} or not url.hostname or url.port == 0
                or url.username is not None or url.password is not None or url.query or url.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError
    except ValueError:
        raise ValueError("Hub URL must be a ws:// or wss:// endpoint without credentials, query, or fragment") from None
    return url


def docker_hub_url(host_url):
    url = hub_endpoint(host_url)
    if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return host_url
    host = "host.docker.internal"
    if url.port is not None:
        host += f":{url.port}"
    return url._replace(netloc=host).geturl()


def validate_identity_and_ports(ids, bff_port, world_port):
    if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in ids):
        raise ValueError("User, Agent, Human and World IDs must be explicit ASCII identifiers")
    if (any(type(port) is not int or not 1 <= port <= 65535 for port in (bff_port, world_port))
            or bff_port == world_port):
        raise ValueError("BFF and World need distinct valid ports")


def initialize(state_dir, *, user_id="local-user", agent_id="quantum-agent-local",
               human_id="human-bff.quantum.local", world_id=None, hub_url=None,
               world_hub_url=None, world_config=None, current_best=0.0, bff_port=8320, world_port=8321):
    state_dir = Path(state_dir).expanduser().resolve()
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("Initialization refuses to overwrite a nonempty state directory")
    if not math.isfinite(current_best) or current_best < 0:
        raise ValueError("current-best must be finite and nonnegative")

    external = world_config is not None
    if external:
        world_config = Path(world_config).expanduser().resolve()
        existing_hub = tomllib.loads(world_config.read_text(encoding="utf-8")).get("hub", {})
        if user_id not in existing_hub.get("allowed_agents", {}).get(agent_id, []):
            raise ValueError("Existing World config must authorize the selected Agent and user")
        existing_id = existing_hub.get("world_id", "quantum-world-local")
        existing_url = existing_hub.get("url", "ws://127.0.0.1:8000")
        if world_id not in {None, existing_id} or world_hub_url not in {None, existing_url}:
            raise ValueError("Existing World ID and Hub URL come from its configuration")
        world_id, world_hub_url = existing_id, existing_url
        hub_url = hub_url or existing_url
    else:
        world_config = state_dir / "world.toml"
        world_id = world_id or "quantum-world-local"
        hub_url = hub_url or "ws://127.0.0.1:8000"
        world_hub_url = world_hub_url or docker_hub_url(hub_url)

    validate_identity_and_ports((user_id, agent_id, human_id, world_id), bff_port, world_port)
    hub_endpoint(hub_url)
    hub_endpoint(world_hub_url)

    token, access_code, salt = secrets.token_urlsafe(48), secrets.token_urlsafe(18), secrets.token_bytes(24)
    account = {
        "userId": user_id, "displayName": "Quantum 操作员", "enabled": True, "salt": salt.hex(),
        "passwordHash": hashlib.pbkdf2_hmac("sha256", access_code.encode(), salt, 200_000).hex(),
    }
    agent = {
        "ownerId": user_id, "agentId": agent_id, "endpointId": agent_id,
        "name": "Quantum Agent", "worldId": world_id, "environmentId": world_id,
        "interactionModes": ["conversation"],
        "capabilities": {"textInput": True, "imageInput": False, "audioInput": False,
                         "appendInput": False, "cancelRun": False, "stopExpression": False,
                         "outputMode": "message", "idempotentInputs": True},
    }
    world = {"ownerId": user_id, "alias": "quantum", "name": "Quantum World", "worldId": world_id,
             "environmentId": world_id, "endpointId": world_id, "humanClientId": human_id + ".tools",
             "humanContext": "envelope", "allowedTools": TOOLS}
    config = {"schema": 1, "user_id": user_id, "agent_id": agent_id, "human_id": human_id,
              "hub_url": hub_url, "world_id": world_id, "world_config": str(world_config),
              "bff_port": bff_port, "world_port": world_port}

    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    state_dir.chmod(0o700)
    private_write(state_dir / "operator-token", token + "\n")
    private_write(state_dir / "login-code", access_code + "\n")
    for name, value in (("accounts", [account]), ("agents", [agent]), ("worlds", [world]), ("console", config)):
        private_write(state_dir / f"{name}.json", json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if not external:
        private_write(world_config, f'''project_root = "/project"
data_root = "/state/world"
worker_python = "/opt/quantum-worker/bin/python"
seed_path = "/project/official/data/sample_submission"
current_best = {current_best}

[hub]
url = {json.dumps(world_hub_url)}
world_id = {json.dumps(world_id)}

[hub.allowed_agents]
{json.dumps(agent_id)} = [{json.dumps(user_id)}]
''')
    return config


def serve_bff(state_dir, *, root=ROOT, environ=None):
    source = os.environ if environ is None else environ
    hub_token = source.get("HUB_CLIENT_TOKEN", "").strip()
    if not hub_token:
        raise ValueError("Set HUB_CLIENT_TOKEN before starting the BFF")
    config = json.loads((state_dir / "console.json").read_text(encoding="utf-8"))
    if config.get("schema") != 1:
        raise ValueError("Unsupported console configuration schema")
    validate_identity_and_ports((config[key] for key in ("user_id", "agent_id", "human_id", "world_id")),
                                config["bff_port"], config["world_port"])
    hub_endpoint(config["hub_url"])
    token = (state_dir / "operator-token").read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("Invalid operator service token")
    frontend = root / "frontend"
    for path in (frontend / "index.html", *(state_dir / f"{name}.json" for name in ("accounts", "agents", "worlds"))):
        if not path.is_file():
            raise ValueError(f"Missing required console file: {path}")
    run_dir = state_dir / "run-bff"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    if (run_dir / ".env").exists():
        raise ValueError("The BFF run directory must be free of .env files")

    public = f"http://127.0.0.1:{config['bff_port']}"
    child = {key: value for key, value in source.items() if key.upper() in HOST_ENVIRONMENT or key.startswith("LC_")}
    child.update({
        "STAR_BFF_ENVIRONMENT": "local", "STAR_BFF_AUTH_MODE": "development",
        "STAR_BFF_COOKIE_SECURE": "false", "STAR_BFF_PUBLIC_BASE_URL": public,
        "STAR_BFF_UI_ORIGIN": public, "STAR_BFF_DATA_DIR": str(state_dir / "bff"),
        "STAR_BFF_DEVELOPMENT_ACCOUNTS_FILE": str(state_dir / "accounts.json"),
        "STAR_BFF_AGENT_DIRECTORY_FILE": str(state_dir / "agents.json"),
        "STAR_BFF_WORLD_DIRECTORY_FILE": str(state_dir / "worlds.json"),
        "STAR_BFF_TRANSPORT_MODE": "sp", "STAR_BFF_HUMAN_CLIENT_ID": config["human_id"],
        "STAR_BFF_HUB_CLIENT_TOKEN": hub_token, "STAR_BFF_HUB_URL": config["hub_url"],
        "STAR_BFF_HUB_ENVIRONMENT_ID": config["world_id"],
        "STAR_BFF_USER_CONNECTIONS_ENABLED": "false", "STAR_BFF_HUB_REGISTRATION_ONLY": "false",
        "QUANTUM_BFF_OPERATOR_URL": f"http://127.0.0.1:{config['world_port']}",
        "QUANTUM_BFF_OPERATOR_TOKEN": token, "QUANTUM_BFF_READ_ONLY": "true",
        "QUANTUM_BFF_FRONTEND_DIR": str(frontend),
    })
    command = [sys.executable, "-m", "quantum_bff.cli", "--host", "127.0.0.1", "--port", str(config["bff_port"])]
    return subprocess.run(command, cwd=run_dir, env=child, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".local-console")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create configuration and a development account once")
    init.add_argument("--user-id", default="local-user")
    init.add_argument("--agent-id", default="quantum-agent-local")
    init.add_argument("--human-id", default="human-bff.quantum.local")
    init.add_argument("--world-id")
    init.add_argument("--hub-url", help="Hub URL from the host BFF")
    init.add_argument("--world-hub-url", help="Hub URL from the Linux World container")
    init.add_argument("--world-config", type=Path)
    init.add_argument("--current-best", type=float, default=0.0)
    init.add_argument("--bff-port", type=int, default=8320)
    init.add_argument("--world-port", type=int, default=8321)
    commands.add_parser("serve-bff", help="Run the read-only competition BFF on loopback")
    options = vars(parser.parse_args())
    state_dir = options.pop("state_dir").expanduser().resolve()
    command = options.pop("command")
    try:
        if command == "serve-bff":
            return serve_bff(state_dir)
        config = initialize(state_dir, **options)
        print(f"Initialized {state_dir / 'console.json'}")
        print(f"Login user {config['user_id']}. Access code file {state_dir / 'login-code'}")
    except (OSError, ValueError, KeyError) as error:
        print(f"Console: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
