"""Generate local-only observability credentials without printing secrets. Run from repo root."""

import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key


def main():
    path = Path(".env.observability")
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    values = dotenv_values(path)
    defaults = {
        key: secrets.token_hex(32)
        for key in (
            "OBS_POSTGRES_PASSWORD",
            "OBS_CLICKHOUSE_PASSWORD",
            "OBS_REDIS_PASSWORD",
            "OBS_MINIO_PASSWORD",
            "OBS_NEXTAUTH_SECRET",
            "OBS_SALT",
            "OBS_ENCRYPTION_KEY",
            "OBS_USER_PASSWORD",
        )
    }
    defaults.update(
        LANGFUSE_PUBLIC_KEY="pk-lf-" + secrets.token_hex(16),
        LANGFUSE_SECRET_KEY="sk-lf-" + secrets.token_hex(32),
        OBS_USER_EMAIL="admin@hellocounsel.local",
    )
    for key, value in defaults.items():
        if not values.get(key):
            set_key(path, key, value)
    values = dotenv_values(path)
    app_env = Path(".env")
    app_env.touch(mode=0o600, exist_ok=True)
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        set_key(app_env, key, values[key])
    for key, value in {
        "OTEL_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
        "LANGFUSE_BASE_URL": "http://localhost:3000",
    }.items():
        set_key(app_env, key, value)
    print("Local credentials saved in .env.observability (not printed).")
    print("Langfuse: http://localhost:3000 — login email admin@hellocounsel.local")
    print("Read OBS_USER_PASSWORD in .env.observability for the login password.")
    print("Jaeger: http://localhost:16686")


if __name__ == "__main__":
    main()
