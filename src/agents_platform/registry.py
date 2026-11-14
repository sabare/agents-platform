import hashlib
import json
from importlib.resources import files


def load_definition(agent_key: str, version: int) -> dict:
    # Only application-owned registry keys are accepted; never interpret user paths.
    if (agent_key, version) not in {
        ("medical_records_collector", 1),
        ("medical_records_collector", 2),
        ("medical_records_collector", 3),
        ("case_supervisor", 1),
        ("case_supervisor", 2),
        ("client_check_in", 1),
    }:
        raise ValueError("Unknown agent definition")
    resource = files("agents_platform").joinpath("definitions", f"{agent_key}.v{version}.json")
    return json.loads(resource.read_text())


def definition_hash(definition: dict) -> str:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
