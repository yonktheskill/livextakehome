"""User-invoked dedicated account sign-in. Never run from tests or application startup."""

import os
from importlib.metadata import distribution
from pathlib import Path

from openai_codex import Codex, CodexConfig


def main():
    configured = os.environ.get("LIVEX_CODEX_AUTH_HOME", "")
    home = Path(configured)
    if not configured or not home.is_absolute():
        raise SystemExit(
            "Set LIVEX_CODEX_AUTH_HOME to a dedicated absolute directory first."
        )
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    runtime = distribution("openai-codex-cli-bin")
    if runtime.version != "0.163.0":
        raise SystemExit("Install the pinned runtime using make setup-codex.")
    binary = next(
        runtime.locate_file(file)
        for file in runtime.files or []
        if str(file).endswith("/bin/codex")
    )
    args = (
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        f"CODEX_HOME={home}",
        "PATH=/usr/bin:/bin",
        str(binary),
        "app-server",
        "--strict-config",
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'forced_login_method="chatgpt"',
    )
    with Codex(CodexConfig(launch_args_override=args, cwd=str(home))) as client:
        login = client.login_chatgpt_device_code()
        print("Complete account sign-in privately using this URL and device code:")
        print(login.verification_url, login.user_code)
        if not login.wait().success:
            raise SystemExit("Account login did not complete.")
    print("Dedicated account login saved. No inference was started.")


if __name__ == "__main__":
    main()
