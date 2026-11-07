"""Create local-only credentials without printing them. Does not start services."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    print(".env already exists; preserved")
else:
    admin, app, worker = (secrets.token_urlsafe(32) for _ in range(3))
    data = (
        f"AKB_POSTGRES_PASSWORD={admin}\n"
        f"AKB_DATABASE_URL=postgresql+psycopg://akb_app:{app}@127.0.0.1:55471/akb\n"
        f"AKB_WORKER_DATABASE_URL=postgresql+psycopg://akb_worker:{worker}@127.0.0.1:55471/akb\n"
        f"AKB_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:{admin}@127.0.0.1:55471/akb\n"
        "AKB_STORAGE_DIR=./data/objects\nAKB_WEB_ORIGIN=http://localhost:5877\n"
        "AKB_API_URL=http://127.0.0.1:5876\n"
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    print("Created private local .env")
