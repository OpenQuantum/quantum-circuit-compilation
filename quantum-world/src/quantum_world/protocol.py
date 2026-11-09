"""Real Star Protocol EnvironmentClient; JSON summaries travel in data.text."""

import json
from star_protocol import EnvironmentClient, ProtocolConflictError
from .store import WorldError, canonical, identifier
from .tools import DEFINITIONS


class QuantumEnvironment(EnvironmentClient):
    def __init__(self, world):
        super().__init__(world.settings.world_id)
        self.world = world
        self.configure_capabilities(tools=DEFINITIONS, events=[])

    async def on_action(self, sender, content):
        try:
            outcome = await self.world.call(
                sender, content["id"], content["name"], content["params"]
            )
        except WorldError as error:
            if error.code == "IDEMPOTENCY_CONFLICT":
                raise ProtocolConflictError(error.message) from None
            user = content.get("params", {}).get("userId")
            owner = (
                canonical([sender, user])
                if isinstance(user, str) and len(user) <= 256
                else canonical([sender, "__invalid__"])
            )
            request = canonical({"name": content["name"], "params": content["params"]})
            with self.world.store.transaction():
                previous = self.world.store.action(owner, content["id"])
                if previous:
                    if previous[0] != request:
                        raise ProtocolConflictError("Action id conflict")
                    outcome = previous[1]
                else:
                    outcome = {
                        "id": identifier("outcome"),
                        "refs": [content["id"]],
                        "status": "rejected",
                        "data": {
                            "text": json.dumps(
                                {
                                    "error": {
                                        "code": error.code,
                                        "message": error.message,
                                    }
                                }
                            )
                        },
                    }
                    self.world.store.save_action(owner, content["id"], request, outcome)
        await self.send_outcome(sender, outcome)
