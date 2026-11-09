"""Exercise real Docker generation and official verification. Never calls Kaggle."""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from quantum_world.config import Settings
from quantum_world.service import World
from quantum_world.store import WorldError, identifier


async def run(args):
    settings = Settings.load(args.config)
    settings = replace(
        settings,
        data_root=settings.project_root
        / ".quantum-world-smoke"
        / ("docker" if args.docker else "native"),
        seed_path=settings.project_root / "official/data/sample_submission",
    )
    world = World(settings)

    async def call(name, params):
        result = await world.call(
            "quantum-agent-local",
            identifier("smoke-action"),
            name,
            {"userId": "local-user", **params},
        )
        return json.loads(result["data"]["text"])

    evidence = {
        "mode": args.mode,
        "docker": args.docker,
        "kaggle_called": False,
        "experiments": [],
    }
    try:
        for task in args.tasks:
            candidate = {}
            if args.docker:
                c = await call(
                    "CreateCandidate",
                    {
                        "task": task,
                        "files": {
                            "main.py": Path(__file__)
                            .with_name("identity.py")
                            .read_text()
                        },
                        "entrypoint": "main.py",
                    },
                )
                candidate["candidate_id"] = c["candidate_id"]
            queued = await call(
                "RunExperiment", {"task": task, "mode": args.mode, **candidate}
            )
            print(json.dumps(queued), flush=True)
            await world.workers[queued["experiment_id"]]
            report = await call(
                "GetExperiment", {"experiment_id": queued["experiment_id"]}
            )
            evidence["experiments"].append(report)
            print(json.dumps(report), flush=True)
            if report["status"] != "succeeded":
                raise RuntimeError(f"{task} failed; inspect report")
        if args.mode == "full" and set(args.tasks) == {"invsqrt", "dqi", "dfthcsos"}:
            try:
                evidence["package"] = await call(
                    "BuildSubmission",
                    {"experiments": [e["id"] for e in evidence["experiments"]]},
                )
            except WorldError as error:
                if error.code != "NO_IMPROVEMENT":
                    raise
                evidence["package_blocked"] = error.code
        path = (
            Path(__file__).parents[1]
            / "validation"
            / ("baseline-" + args.mode + ("-docker" if args.docker else "") + ".json")
        )
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print("Evidence:", path)
    finally:
        await world.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument(
        "--tasks",
        nargs="+",
        choices=["invsqrt", "dqi", "dfthcsos"],
        default=["invsqrt"],
    )
    asyncio.run(run(parser.parse_args()))
