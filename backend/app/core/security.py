import hashlib
import json
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return passwords.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token() -> str:
    return secrets.token_urlsafe(32)


class KeyVault:
    def __init__(self, master: str, previous: list[str] | None = None):
        try:
            self.fernet = MultiFernet([Fernet(key.encode()) for key in [master, *(previous or [])]])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "MASTER_SECRET must be a deployment-supplied Fernet key. Generate one explicitly "
                "with cryptography.fernet.Fernet.generate_key(); never bake it into an image."
            ) from exc

    def seal(self, key: str, user: int, owner: int) -> str:
        payload = json.dumps({"key": key, "user": user, "owner": owner, "version": 1})
        return self.fernet.encrypt(payload.encode()).decode()

    def open(self, ciphertext: str, user: int, owner: int) -> str:
        try:
            data = json.loads(self.fernet.decrypt(ciphertext.encode()))
            if data["user"] != user or data["owner"] != owner or data["version"] != 1:
                raise ValueError("identity mismatch")
            return data["key"]
        except (InvalidToken, ValueError, KeyError) as exc:
            raise RuntimeError("Connection decryption failed; reconnect in settings.") from exc


SENSITIVE = {"api_key", "key", "authorization", "cookie", "password", "token", "secret"}


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]" if any(s in k.lower() for s in SENSITIVE) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # No raw model prompts, free-form exceptions, or response bodies are traced.
        return value[:4000]
    return value
