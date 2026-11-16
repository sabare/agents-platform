from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://agents:agents@localhost:5432/agents"
    migration_database_url: str | None = None
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_ui_url: str = "http://localhost:8233"
    temporal_task_queue: str = "agents-platform"
    log_level: str = "INFO"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    otel_environment: str = "local"
    service_version: str = "0.1.0"
    jaeger_ui_url: str = "http://localhost:16686"
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_ui_url: str = "http://localhost:3000"
    langfuse_project_id: str = "hellocounsel-agents"
    langfuse_content_mode: Literal["redacted", "local_debug"] = "redacted"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    operator_api_key: SecretStr = SecretStr("local-development-only")
    agent_mode: Literal["scripted", "openai"] = "scripted"
    openai_model: str = "gpt-6-luna"
    context_input_tokens: int = Field(default=12000, ge=2048, le=64000)
    context_output_tokens: int = Field(default=1500, ge=256, le=8000)
    context_retrieval_calls: int = Field(default=4, ge=1, le=10)
    demo_assume_routine_updates: bool = False
    demo_follow_up_seconds: int = Field(default=30, ge=1, le=86400)
    ops_poll_seconds: int = Field(default=15, ge=1, le=300)
    ops_outbox_age_seconds: int = Field(default=60, ge=1)
    ops_worker_delay_seconds: int = Field(default=120, ge=1)
    ops_callback_grace_seconds: int = Field(default=60, ge=0)
    ops_review_age_seconds: int = Field(default=86400, ge=1)
    demo_max_activations: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def local_trace_content(self):
        from urllib.parse import urlparse

        if self.langfuse_content_mode == "local_debug" and (
            self.otel_environment != "local"
            or urlparse(self.langfuse_base_url).hostname
            not in {"localhost", "127.0.0.1", "::1", "host.docker.internal", "langfuse-web"}
        ):
            raise ValueError("Readable trace content requires local Langfuse and environment=local")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def agent_model_settings(model, output_tokens, *, temperature=None):
    from agents import ModelSettings
    from openai.types.shared import Reasoning

    options = {"max_tokens": output_tokens}
    if isinstance(model, str) and model.startswith("gpt-6"):
        options["reasoning"] = Reasoning(effort="none")
    if temperature is not None:
        options["temperature"] = temperature
    return ModelSettings(**options)
