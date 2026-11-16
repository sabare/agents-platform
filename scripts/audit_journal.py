"""Verify/export one tenant stream. Copy exported checkpoints outside the database host."""

import argparse
import json
from pathlib import Path
from uuid import UUID

import psycopg
from sqlalchemy.engine import make_url

from agents_platform.audit_integrity import verify
from agents_platform.config import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant_id", type=UUID)
    parser.add_argument("--export", type=Path, help="Write verified JSONL archive and checkpoint")
    parser.add_argument(
        "--checkpoint", type=Path, help="Compare against a previously saved checkpoint"
    )
    args = parser.parse_args()
    expected = json.loads(args.checkpoint.read_text()) if args.checkpoint else None
    if expected and expected["stream_id"] != str(args.tenant_id):
        parser.error("Checkpoint belongs to a different stream")
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        checkpoint, rows = verify(connection, args.tenant_id, expected)
    if args.export:
        # Exclusive creation prevents silently overwriting prior evidence.
        checkpoint_path = args.export.with_suffix(args.export.suffix + ".checkpoint.json")
        if args.export.exists() or checkpoint_path.exists():
            parser.error("Export or checkpoint already exists")
        with args.export.open("x") as archive:
            for row in rows:
                archive.write(json.dumps(row) + "\n")
        with checkpoint_path.open("x") as target:
            json.dump(checkpoint, target, indent=2)
        args.export.chmod(0o600)
        checkpoint_path.chmod(0o600)
    print(json.dumps({"verified": True, "events": len(rows), **checkpoint}, indent=2))


if __name__ == "__main__":
    main()
