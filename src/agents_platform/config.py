from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://agents:agents@localhost:5432/agents"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agents-platform"
    log_level: str = "INFO"
    operator_api_key: SecretStr = SecretStr("local-development-only")
    agent_mode: Literal["scripted", "openai"] = "scripted"
    context_input_tokens: int = Field(default=12000, ge=2048, le=64000)
    context_output_tokens: int = Field(default=1500, ge=256, le=8000)
    context_retrieval_calls: int = Field(default=4, ge=1, le=10)
    demo_follow_up_seconds: int = Field(default=30, ge=1, le=86400)
    demo_max_activations: int = Field(default=6, ge=1, le=100)


@lru_cache
def get_settings() -> Settings:
    return Settings()
