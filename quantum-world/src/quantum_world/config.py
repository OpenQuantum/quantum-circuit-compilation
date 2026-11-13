from dataclasses import dataclass, field
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
    research_download_token: str | None = field(default=None, repr=False)

    def __post_init__(self):
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
        for name in ("project_root", "data_root", "worker_python", "seed_path"):
            data[name] = path.parent / data[name]
        return cls(
            **data,
            hub_url=hub.get("url", "ws://127.0.0.1:8000"),
            world_id=hub.get("world_id", "quantum-world-local"),
            allowed_agents=hub.get("allowed_agents", {}),
            research_url=research.get("url"),
            research_token=os.environ.get("QUANTUM_RESEARCH_TOKEN"),
            research_download_token=os.environ.get("QUANTUM_RESEARCH_DOWNLOAD_TOKEN"),
        )
