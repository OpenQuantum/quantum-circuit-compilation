"""World daemon, offline calls and human-only submission administration."""

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import signal
import sys

from .config import Settings
from .service import World
from .store import WorldError, identifier


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def run(args):
    settings = Settings.load(args.config)
    world = World(
        settings,
        administrative=args.command in ("approve", "submit", "inspect-package"),
    )
    try:
        if args.command == "serve":
            from .protocol import QuantumEnvironment

            token = (
                getpass.getpass("Hub token: ")
                if args.prompt_token
                else os.environ.get("HUB_CLIENT_TOKEN")
            )
            if not token:
                raise WorldError(
                    "TOKEN_REQUIRED", "Set HUB_CLIENT_TOKEN or use --prompt-token"
                )
            endpoint = QuantumEnvironment(world)
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stopped.set)
            try:
                async with asyncio.timeout(20):
                    await endpoint.connect(
                        settings.hub_url, headers={"Authorization": "Bearer " + token}
                    )
                    await endpoint.start()
                show(
                    {
                        "world_id": settings.world_id,
                        "status": "connected",
                        "approval_required": True,
                    }
                )
                await stopped.wait()
            finally:
                await endpoint.stop()
        elif args.command == "call":
            params = json.loads(args.params)
            params["userId"] = args.user
            outcome = await world.call(
                args.agent,
                args.action_id or identifier("local-action"),
                args.tool,
                params,
            )
            show(outcome)
            if args.tool == "RunExperiment":
                job_id = json.loads(outcome["data"]["text"])["experiment_id"]
                worker = world.workers.get(job_id)
                if worker:
                    await worker
                show(
                    await world.call(
                        args.agent,
                        identifier("local-read"),
                        "GetExperiment",
                        {"userId": args.user, "experiment_id": job_id},
                    )
                )
        elif args.command == "inspect-package":
            from .approval import package

            show(package(world, args.package_id)[1])
        elif args.command == "approve":
            from .approval import approve, package

            p = package(world, args.package_id)[1]
            show(p)
            if not sys.stdin.isatty():
                raise WorldError(
                    "HUMAN_REQUIRED", "Approval requires an interactive human terminal"
                )
            print(
                "Only approve after reviewing all three full verification reports and archive contents."
            )
            expected = "APPROVE " + p["sha256"]
            answer = input("Type " + expected + "\n> ")
            if answer != expected:
                raise WorldError(
                    "APPROVAL_DECLINED", "Exact approval phrase was not entered"
                )
            show(
                approve(
                    world,
                    args.package_id,
                    args.operator or getpass.getuser(),
                    p["sha256"],
                )
            )
        elif args.command == "submit":
            from .approval import submit
            from .gateway import KaggleGateway

            show(await submit(world, args.package_id, KaggleGateway(settings)))
    finally:
        await world.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--prompt-token", action="store_true")
    call = commands.add_parser(
        "call", help="Offline operator test; RunExperiment waits for completion"
    )
    call.add_argument("tool")
    call.add_argument(
        "--params", required=True, help="JSON arguments; userId supplied separately"
    )
    call.add_argument("--agent", default="quantum-agent-local")
    call.add_argument("--user", default="local-user")
    call.add_argument("--action-id")
    for name in ("inspect-package", "approve", "submit"):
        command = commands.add_parser(name)
        command.add_argument("package_id")
        if name == "approve":
            command.add_argument("--operator")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (WorldError, ValueError, FileNotFoundError) as error:
        show(
            {
                "error": {
                    "code": getattr(error, "code", "CONFIG_ERROR"),
                    "message": str(error),
                }
            }
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
