"""Versioned definitions discovered only from the installed application package."""

import hashlib
import json
import re
from importlib.resources import files


def packaged_definitions() -> list[dict]:
    definitions = []
    directory = files("agents_platform").joinpath("definitions")
    for resource in sorted(directory.iterdir(), key=lambda r: r.name):
        match = re.fullmatch(r"([a-z][a-z0-9_]*)\.v([1-9][0-9]*)\.json", resource.name)
        if not match:
            continue
        definition = json.loads(resource.read_text())
        if (definition["agent_key"], definition["version"]) != (match[1], int(match[2])):
            raise ValueError(f"Definition identity does not match {resource.name}")
        definitions.append(definition)
    return definitions


def load_definition(agent_key: str, version: int) -> dict:
    # Compare identities rather than interpreting a caller-supplied path.
    for definition in packaged_definitions():
        if (definition["agent_key"], definition["version"]) == (agent_key, version):
            return definition
    raise ValueError("Unknown agent definition")


def definition_hash(definition: dict) -> str:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def publish_definitions(session, catalog=None):
    """Publish deployed definitions without overwriting any existing version; caller commits."""
    from sqlalchemy.dialects.postgresql import insert

    from agents_platform.capabilities import discover_capabilities
    from agents_platform.models import AgentVersion

    catalog = catalog or discover_capabilities()
    definitions = {(d["agent_key"], d["version"]): d for d in packaged_definitions()}
    for capability in catalog.capabilities.values():
        definition = capability.definition
        identity = (definition["agent_key"], definition["version"])
        if identity in definitions and definitions[identity] != definition:
            raise ValueError("Plugin and packaged definition disagree")
        definitions[identity] = definition
    for (key, version), definition in sorted(definitions.items()):
        fingerprint = definition_hash(definition)
        await session.execute(
            insert(AgentVersion)
            .values(
                agent_key=key,
                version=version,
                definition_hash=fingerprint,
                definition_snapshot=definition,
            )
            .on_conflict_do_nothing()
        )
        row = await session.get(AgentVersion, (key, version))
        if (
            row.definition_hash != fingerprint
            or definition_hash(row.definition_snapshot) != fingerprint
        ):
            raise ValueError("Published agent version changed; publish a new version explicitly")
