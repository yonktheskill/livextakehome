"""Offline, real-HTTP verification of the built two-container application.

Uses fake inference and a disposable database. Never reads project dotenv files.
Build images first with Compose; all test containers/network are removed on exit.
"""

import base64
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def ready(base):
    for _ in range(50):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.HTTPError):
            time.sleep(0.2)
    raise RuntimeError("Application did not become healthy")


def main():
    suffix = uuid.uuid4().hex[:10]
    network, backend, frontend = [
        f"daylight-smoke-{name}-{suffix}" for name in ("net", "api", "ui")
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    password = secrets.token_urlsafe(24)
    email = f"smoke-{suffix}@example.com"
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def request(path, body=None, method=None, origin=base):
        req = urllib.request.Request(
            base + "/api" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        with client.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    with tempfile.TemporaryDirectory(prefix="daylight-smoke-") as directory:
        env = Path(directory) / "backend.env"
        env.write_text(
            "MASTER_SECRET="
            + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            + f"\nPUBLIC_ORIGIN={base}\nLLM_PROVIDER=fake\nCOOKIE_SECURE=false\n"
        )
        os.chmod(env, 0o600)
        try:
            docker("network", "create", network)
            docker(
                "run",
                "-d",
                "--name",
                backend,
                "--network",
                network,
                "--network-alias",
                "backend",
                "--env-file",
                str(env),
                "livextakehome-backend:latest",
            )
            docker(
                "run",
                "-d",
                "--name",
                frontend,
                "--network",
                network,
                "-p",
                f"127.0.0.1:{port}:3000",
                "livextakehome-frontend:latest",
            )
            ready(base)
            with urllib.request.urlopen(base + "/login", timeout=10) as response:
                assert response.status == 200 and b"Daylight" in response.read()
            user = request("/auth/register", {"email": email, "password": password})
            assert request("/auth/me")["id"] == user["id"]
            assert request("/connection")["connected"] is False
            body = {
                "content": "Hello from the offline container smoke test",
                "client_request_id": str(uuid.uuid4()),
            }
            accepted = request("/chat/messages", body)
            assert request("/chat/messages", body) == accepted
            stream_req = urllib.request.Request(
                base + f"/api/runs/{accepted['run_id']}/events"
            )
            with client.open(stream_req, timeout=30) as response:
                stream = response.read().decode()
            assert "event: message.delta" in stream and "event: run.completed" in stream
            assert request(f"/runs/{accepted['run_id']}")["status"] == "succeeded"
            history = request("/chat/history")["messages"]
            assert any(m["role"] == "assistant" and m["content"] for m in history)
            try:
                request("/auth/logout", method="POST", origin="https://wrong.example")
                raise AssertionError("Cross-origin mutation unexpectedly accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 403
            docker("restart", backend)
            ready(base)
            assert request("/auth/me")["id"] == user["id"]
            assert request("/chat/history")["messages"] == history
            request("/auth/logout", method="POST")
            try:
                request("/auth/me")
                raise AssertionError("Logout did not revoke session")
            except urllib.error.HTTPError as error:
                assert error.code == 401
            print(
                "PASS: container proxy, registration/cookies, dedupe, durable SSE/history, CSRF, restart, logout"
            )
        finally:
            for name in (frontend, backend):
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, check=False
                )
            subprocess.run(
                ["docker", "network", "rm", network], capture_output=True, check=False
            )


if __name__ == "__main__":
    main()
