"""Operator-side Kaggle process. Never imported by the World tool dispatcher."""

import asyncio
import json
import os
from pathlib import Path
from .store import WorldError


class KaggleGateway:
    def __init__(self, settings):
        self.settings = settings

    async def _run(self, action, *arguments):
        # Only the operator command inherits Kaggle credentials. Generators and
        # native verifiers use their own minimal environments.
        command = [
            str(self.settings.worker_python),
            str(Path(__file__).with_name("kaggle_gateway.py")),
            action,
            *map(str, arguments),
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(3600 if action == "upload" else 120):
                output, _ = await proc.communicate()
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise WorldError(
                "KAGGLE_ERROR", "Kaggle operation failed; no automatic retry."
            )
        try:
            return json.loads(output)
        except (ValueError, UnicodeError):
            raise WorldError(
                "KAGGLE_ERROR", "Invalid Kaggle response; reconcile account history."
            ) from None

    async def best(self):
        return (await self._run("best"))["best"]

    async def upload(self, archive, folder):
        return await self._run(
            "upload",
            archive,
            folder / "receipt.json",
            self.settings.data_root,
            folder.name,
        )
