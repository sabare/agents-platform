"""Give local API/worker a restricted login. Run after migrations as the database owner."""

import secrets
from pathlib import Path

import psycopg
from dotenv import dotenv_values, set_key
from sqlalchemy.engine import make_url

from agents_platform.audit_roles import provision


def main():
    path = Path(".env")
    values = dotenv_values(path)
    admin_url = make_url(
        values.get("MIGRATION_DATABASE_URL")
        or values.get("DATABASE_URL")
        or "postgresql+psycopg://agents:agents@localhost:5432/agents"
    )
    role = values.get("DATABASE_APP_USER") or "agents_app"
    password = values.get("DATABASE_APP_PASSWORD") or secrets.token_hex(32)
    with psycopg.connect(
        admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    ) as connection:
        provision(connection, role, password)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    for key, value in {
        "MIGRATION_DATABASE_URL": admin_url.render_as_string(hide_password=False),
        "DATABASE_URL": admin_url.set(username=role, password=password).render_as_string(
            hide_password=False
        ),
        "DATABASE_APP_USER": role,
        "DATABASE_APP_PASSWORD": password,
    }.items():
        set_key(path, key, value)
    print("Restricted API/worker login configured. Credentials saved in .env, not printed.")


if __name__ == "__main__":
    main()
