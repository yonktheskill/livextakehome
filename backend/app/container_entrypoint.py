"""Initialize mounted storage, then run the server as the unprivileged app user."""

import json
import os
import pwd
import sys
from pathlib import Path


def provision_codex_login():
    """Consume a temporary deployment secret into private persistent storage."""
    login = os.environ.pop("CODEX_LOGIN_BOOTSTRAP", None)
    if not login:
        return
    home = Path(os.environ["LIVEX_CODEX_AUTH_HOME"])
    if not home.is_absolute():
        raise ValueError("Codex login directory must be absolute")
    if not isinstance(json.loads(login), dict):
        raise ValueError("Invalid Codex login format")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = home / "auth.json"
    # Never replace an existing login, including a symlink.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as stream:
        stream.write(login)


def main():
    provision_codex_login()
    if os.getuid() == 0:
        account = pwd.getpwnam("app")
        data = Path(os.environ.get("DATABASE_PATH", "/app/data/app.sqlite3")).parent
        paths = [data]
        if auth_home := os.environ.get("LIVEX_CODEX_AUTH_HOME"):
            paths.append(Path(auth_home))
        for path in paths:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(path, account.pw_uid, account.pw_gid)
            os.chmod(path, 0o700)
        if auth_home:
            auth = Path(auth_home) / "auth.json"
            if auth.exists():
                os.chown(auth, account.pw_uid, account.pw_gid)
                os.chmod(auth, 0o600)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
