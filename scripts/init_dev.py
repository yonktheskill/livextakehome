"""Create local-only encryption configuration without printing or loading credentials."""

import base64
import os
import secrets
from pathlib import Path

destination = Path(__file__).resolve().parents[1] / ".env.local"
if destination.exists():
    raise SystemExit(".env.local already exists; left unchanged.")
key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
    file.write(
        f"MASTER_SECRET={key}\n"
        "DATABASE_PATH=data/app.sqlite3\n"
        "PUBLIC_ORIGIN=http://localhost:3000\n"
        "COOKIE_SECURE=false\n"
        "LLM_PROVIDER=codex\n"
        "BACKEND_URL=http://localhost:8000\n"
    )
print("Created private .env.local. Select a usable provider before starting chat.")
