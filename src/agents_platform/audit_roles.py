"""Local database bootstrap, executed only with migration credentials."""

import os

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from agents_platform import models  # noqa: F401
from agents_platform.config import get_settings
from agents_platform.db import Base


def provision(connection, role, password):
    exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    if not exists:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    connection.execute(
        sql.SQL(
            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(connection.info.dbname), sql.Identifier(role)
        )
    )
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
    for table in Base.metadata.sorted_tables:
        connection.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE {} TO {}").format(
                sql.Identifier("public", table.name), sql.Identifier(role)
            )
        )
    connection.execute(
        sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
            sql.Identifier(role)
        )
    )
    connection.execute(
        sql.SQL("GRANT agents_audit_writer,agents_audit_reader TO {}").format(sql.Identifier(role))
    )
    # Do not grant ownership, writer-owner membership, maintenance or schema creation.


def main():
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        provision(
            connection,
            os.getenv("DATABASE_APP_USER", "agents_app"),
            os.getenv("DATABASE_APP_PASSWORD", "local-app-only"),
        )
    print("Restricted application database role is ready.")


if __name__ == "__main__":
    main()
