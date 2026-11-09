"""Agent-facing tools intentionally do NOT include ApproveSubmission or SubmitSubmission."""

from jsonschema import Draft202012Validator


def schema(properties, required=()):
    return {
        "type": "object",
        "properties": {
            "userId": {"type": "string", "minLength": 1, "maxLength": 256},
            **properties,
        },
        "required": ["userId", *required],
        "additionalProperties": False,
    }


ID = {"type": "string", "minLength": 1, "maxLength": 100}
TASK = {"type": "string", "enum": ["invsqrt", "dqi", "dfthcsos"]}
DEFINITIONS = [
    {
        "name": "GetTask",
        "description": "Read quantum task constraints, reference costs and configured comparison score.",
        "parameters": schema({"task": TASK}, ["task"]),
    },
    {
        "name": "CreateCandidate",
        "description": "Create an immutable candidate from UTF-8 generator files. Paths are relative. Entrypoint runs only in an isolated Docker container.",
        "parameters": schema(
            {
                "task": TASK,
                "files": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 16,
                    "additionalProperties": {"type": "string", "maxLength": 65536},
                },
                "entrypoint": {"type": "string", "maxLength": 240},
                "description": {"type": "string", "maxLength": 1000},
            },
            ["task", "files", "entrypoint"],
        ),
    },
    {
        "name": "RunExperiment",
        "description": "Queue a quick or full experiment; immediately returns experiment_id. Quick tests are NEVER eligible for submission.",
        "parameters": schema(
            {
                "task": TASK,
                "candidate_id": ID,
                "mode": {"type": "string", "enum": ["quick", "full"]},
            },
            ["task", "mode"],
        ),
    },
    {
        "name": "GetExperiment",
        "description": "Read an owned experiment status, bounded report and log tail. A completed request does not mean the experiment passed.",
        "parameters": schema({"experiment_id": ID}, ["experiment_id"]),
    },
    {
        "name": "CancelExperiment",
        "description": "Request cancellation of an owned experiment, terminating its process group/container before confirming cancelled.",
        "parameters": schema({"experiment_id": ID}, ["experiment_id"]),
    },
    {
        "name": "BuildSubmission",
        "description": "Build a package from three successful full experiments. Returns awaiting_approval; NEVER uploads to Kaggle.",
        "parameters": schema(
            {
                "experiments": {
                    "type": "array",
                    "items": ID,
                    "minItems": 3,
                    "maxItems": 3,
                    "uniqueItems": True,
                }
            },
            ["experiments"],
        ),
    },
]
VALIDATORS = {d["name"]: Draft202012Validator(d["parameters"]) for d in DEFINITIONS}
