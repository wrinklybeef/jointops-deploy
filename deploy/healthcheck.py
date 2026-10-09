"""Probe existing HTTP serving, PostgreSQL, and migration head; no Microsoft calls."""

import os
import sys
from urllib.request import urlopen

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool


def main():
    with urlopen("http://127.0.0.1:8000/", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("HTTP probe failed")
    engine = create_engine(
        os.environ["JOINTOPS_DATABASE_URL"],
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            if connection.scalar(text("SELECT 1")) != 1:
                raise RuntimeError("Database probe failed")
            current = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
        expected = set(ScriptDirectory.from_config(Config("/app/alembic.ini")).get_heads())
        if current != expected:
            raise RuntimeError("Migration head mismatch")
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Docker records health output; do not expose DB URLs or driver exceptions.
        print("Joint Ops HTTP/database/schema health check failed", file=sys.stderr)
        sys.exit(1)
