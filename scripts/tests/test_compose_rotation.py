"""Resolve Compose locally without starting containers or reading user secrets."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is unavailable")
class ComposeRotationTests(unittest.TestCase):
    def test_explicit_previous_keys_override_env_file_and_stay_backend_only(self):
        if subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode:
            self.skipTest("Docker Compose is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Non-secret synthetic values used only for configuration resolution.
            current = "A" * 43 + "="
            previous = "B" * 43 + "="
            (root / ".env.local").write_text(
                f"MASTER_SECRET={current}\nMASTER_SECRET_PREVIOUS=[]\n"
            )
            environment = dict(os.environ)
            environment.update(
                MASTER_SECRET=current,
                MASTER_SECRET_PREVIOUS=json.dumps([previous]),
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(root),
                    "-f",
                    str(COMPOSE),
                    "config",
                    "--format",
                    "json",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            services = json.loads(result.stdout)["services"]
            backend = services["backend"]["environment"]
            self.assertEqual(backend["MASTER_SECRET"], current)
            self.assertEqual(json.loads(backend["MASTER_SECRET_PREVIOUS"]), [previous])
            self.assertNotIn(
                "MASTER_SECRET_PREVIOUS", services["frontend"]["environment"]
            )
