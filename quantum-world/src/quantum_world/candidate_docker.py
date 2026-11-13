"""Map World-local experiment paths to mounts understood by the Docker daemon."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess

from .store import WorldError


DOCKER_SOCKET = "/var/run/docker.sock"
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
INSPECT_MOUNTS = (
    '{"id":{{json .Id}},"running":{{json .State.Running}},'
    '"configured":{{json .HostConfig.Mounts}},"active":{{json .Mounts}}}'
)


def docker_environment():
    # Ignore host Docker contexts, registry credentials and unrelated service credentials.
    return {"PATH": os.defpath, "DOCKER_CONFIG": "/dev/null"}


def volume_mount(info, path, target, *, readonly, allow_subpath):
    path = path.resolve(strict=True)
    matches = []
    for mount in info["active"]:
        destination = Path(mount["Destination"])
        if destination != path and destination.is_relative_to(path):
            raise WorldError("DOCKER_MOUNT", f"World mounts overlap the candidate's {target} directory")
        if path.is_relative_to(destination):
            matches.append((len(destination.parts), destination, mount))
    if not matches:
        raise WorldError("DOCKER_MOUNT", f"Candidate {target} needs an explicit named volume")
    _, destination, active = max(matches, key=lambda item: item[0])
    configured = next((mount for mount in info["configured"] if mount.get("Target") == str(destination)), {})
    name = configured.get("Source", "")
    if (configured.get("Type") != "volume" or active.get("Type") != "volume" or active.get("Driver") != "local"
            or not NAME.fullmatch(name) or name != active.get("Name")):
        raise WorldError("DOCKER_MOUNT", f"Candidate {target} needs a local named volume mounted with --mount")
    if (configured.get("VolumeOptions") or {}).get("Subpath"):
        raise WorldError("DOCKER_MOUNT", f"World must mount the volume root for candidate {target}")
    # The source directory and outputs both belong to the World state volume.
    # Only the official data mount must already be read-only in World itself.
    if target == "/official" and active.get("RW") is not False:
        raise WorldError("DOCKER_MOUNT", "World official data volume must be mounted read-only")
    if target == "/out" and active.get("RW") is not True:
        raise WorldError("DOCKER_MOUNT", "World state volume must be writable")
    relative = path.relative_to(destination)
    if target == "/official" and relative.parts:
        raise WorldError("DOCKER_MOUNT", "World must mount official data at the volume root")
    if allow_subpath and (not relative.parts or any("," in part or '"' in part for part in relative.parts)):
        raise WorldError("DOCKER_MOUNT", f"Candidate {target} must use a scoped volume subdirectory")
    options = ["type=volume", f"source={name}", f"target={target}", "volume-nocopy"]
    if relative.parts:
        options.append(f"volume-subpath={relative.as_posix()}")
    if readonly:
        options.append("readonly")
    return ["--mount", ",".join(options)], name


def candidate_mounts(programs, official, inputs, environment):
    world = os.environ.get("QUANTUM_DOCKER_SELF")
    if world is None:
        if Path(DOCKER_SOCKET).is_socket() and Path("/.dockerenv").exists():
            raise WorldError("DOCKER_MOUNT", "Set QUANTUM_DOCKER_SELF to enable container volume mapping")
        return ["docker"], ["-v", f"{programs}:/candidate:ro", "-v", f"{official}:/official:ro", "-v", f"{inputs}:/out:rw"]
    if not NAME.fullmatch(world):
        raise WorldError("DOCKER_MOUNT", "QUANTUM_DOCKER_SELF must identify this World container")
    docker = ["docker", "--host", "unix://" + DOCKER_SOCKET]
    try:
        inspection = subprocess.run(
            [*docker, "container", "inspect", "--format", INSPECT_MOUNTS, world],
            env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=10,
        )
        info = json.loads(inspection.stdout)
        hostname = socket.gethostname()
        if (not info["running"] or len(hostname) not in {12, 64} or not info["id"].startswith(hostname)
                or not isinstance(info["configured"], list) or not isinstance(info["active"], list)):
            raise ValueError
        source, state_name = volume_mount(info, programs, "/candidate", readonly=True, allow_subpath=True)
        data, official_name = volume_mount(info, official, "/official", readonly=True, allow_subpath=False)
        output, output_name = volume_mount(info, inputs, "/out", readonly=False, allow_subpath=True)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError, TypeError):
        raise WorldError("DOCKER_MOUNT", "Could not inspect the running World container; use its default Docker hostname") from None
    if state_name != output_name or state_name == official_name:
        raise WorldError("DOCKER_MOUNT", "Use separate state and official volumes, with candidate and output in the state volume")
    return docker, [*source, *data, *output]
