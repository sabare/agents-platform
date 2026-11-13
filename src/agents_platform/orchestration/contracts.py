from dataclasses import dataclass


@dataclass
class ProbeInput:
    delay_seconds: int = 1


@dataclass
class ProbeResult:
    postgres: str
    pgvector_version: str
