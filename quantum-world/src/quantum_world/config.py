from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import math
import os
import tomllib


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_root: Path
    worker_python: Path
    seed_path: Path
    max_workers: int = 2
    job_timeout: int = 3600
    generator_timeout: int = 300
    generator_image: str = "quantum-world-generator:local"
    current_best: float = 0.0
    hub_url: str = "ws://127.0.0.1:8000"
    world_id: str = "quantum-world-local"
    allowed_agents: dict[str, list[str]] = field(default_factory=dict)
    research_url: str | None = None
    research_token: str | None = field(default=None, repr=False)
    invsqrt_demo_started_at: datetime | None = None
    invsqrt_optimization: dict | None = None
    research_results_token: str | None = field(default=None, repr=False)

    def __post_init__(self):
        optimization = self.invsqrt_optimization
        if optimization is not None:
            from .optimization import precision_key

            ids = ("baseline_full_id", "baseline_quick_id")
            if (
                not isinstance(optimization, dict)
                or set(optimization) != {*ids, "baseline_parameters"}
                or any(not isinstance(optimization[key], str) or not 1 <= len(optimization[key]) <= 100
                       for key in ids)
                or optimization[ids[0]] == optimization[ids[1]]
                or not precision_key(optimization["baseline_parameters"])
            ):
                raise ValueError("invsqrt_optimization requires distinct full/quick IDs and five valid precision values")
        if self.research_url:
            url = urlsplit(self.research_url)
            if (
                url.scheme != "http"
                or url.hostname not in ("127.0.0.1", "localhost")
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "research_url must be an explicit loopback HTTP origin without credentials or path"
                )
        for name in ("max_workers", "job_timeout", "generator_timeout"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.current_best) or self.current_best < 0:
            raise ValueError("current_best must be finite and nonnegative")
        demo_start = self.invsqrt_demo_started_at
        if demo_start is not None and (
            not isinstance(demo_start, datetime)
            or demo_start.utcoffset() != timedelta(0)
            or demo_start > datetime.now(timezone.utc)
        ):
            raise ValueError("invsqrt_demo.started_at must be a non-future UTC timestamp")
        if not isinstance(self.allowed_agents, dict) or any(
            not isinstance(agent, str)
            or not agent
            or not isinstance(users, list)
            or not users
            or any(
                not isinstance(user, str) or not 1 <= len(user) <= 256 for user in users
            )
            for agent, users in self.allowed_agents.items()
        ):
            raise ValueError(
                "hub.allowed_agents must map agent IDs to nonempty user ID lists"
            )
        for name in ("project_root", "data_root", "worker_python", "seed_path"):
            path = Path(getattr(self, name)).expanduser()
            # Resolving a venv's Python symlink runs the base interpreter and
            # silently loses that venv's numpy/kickmix installation.
            object.__setattr__(
                self,
                name,
                Path(os.path.abspath(path))
                if name == "worker_python"
                else path.resolve(),
            )

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = tomllib.loads(path.read_text())
        hub = data.pop("hub", {})
        research = data.pop("research", {})
        demo = data.pop("invsqrt_demo", None)
        demo_start = None
        if demo is not None:
            if not isinstance(demo, dict) or set(demo) != {"started_at"}:
                raise ValueError("invsqrt_demo requires only started_at")
            demo_start = demo["started_at"]
            if isinstance(demo_start, str):
                try:
                    demo_start = datetime.fromisoformat(demo_start)
                except ValueError:
                    raise ValueError("Invalid invsqrt_demo.started_at timestamp") from None
        for name in ("project_root", "data_root", "worker_python", "seed_path"):
            data[name] = path.parent / data[name]
        optimization = data.pop("invsqrt_optimization", None)
        return cls(
            **data,
            hub_url=hub.get("url", "ws://127.0.0.1:8000"),
            world_id=hub.get("world_id", "quantum-world-local"),
            allowed_agents=hub.get("allowed_agents", {}),
            research_url=research.get("url"),
            research_token=os.environ.get("QUANTUM_RESEARCH_TOKEN"),
            invsqrt_demo_started_at=demo_start,
            research_results_token=os.environ.get("QUANTUM_RESEARCH_RESULTS_TOKEN"),
            invsqrt_optimization=optimization,
        )
