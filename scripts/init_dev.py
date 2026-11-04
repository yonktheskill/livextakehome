"""Copy the shared settings template, generating private local-only values."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / ".env.local"
if destination.exists():
    raise SystemExit(".env.local already exists; left unchanged.")
overrides = {
    "MASTER_SECRET": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
    "LIVEX_CODEX_AUTH_HOME": str(Path.home() / ".local" / "share" / "livex-codex-login"),
}
lines = []
for line in (root / ".env.example").read_text().splitlines():
    name, separator, _ = line.partition("=")
    lines.append(f"{name}={overrides[name]}" if separator and name in overrides else line)
with os.fdopen(
    os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
) as file:
    file.write("\n".join(lines) + "\n")
print(
    "Created private .env.local. Run make setup-codex and make codex-login before starting chat."
)
