"""Offline subprocess regressions for make dev lifecycle."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SUPERVISOR = Path(__file__).resolve().parents[1] / "dev_supervisor.py"


class SupervisorTests(unittest.TestCase):
    def exercise(self, interrupt):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "fake-make"
            helper.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, signal, subprocess, sys, time\n"
                f"root = pathlib.Path({directory!r})\n"
                "if sys.argv[1] == 'dev-backend':\n"
                "    child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                "    (root / 'pid').write_text(str(child.pid))\n"
                "    def stop(*args):\n"
                "        child.wait(); sys.exit(0)\n"
                "    signal.signal(signal.SIGTERM, stop)\n"
                "else:\n"
                "    while not (root / 'pid').exists(): time.sleep(.01)\n"
                f"    if not {interrupt!r}: sys.exit(7)\n"
                "while True: time.sleep(.01)\n"
            )
            helper.chmod(0o755)
            process = subprocess.Popen([sys.executable, str(SUPERVISOR), str(helper)])
            try:
                deadline = time.monotonic() + 5
                while not (root / "pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / "pid").exists())
                if interrupt:
                    process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=6), 130 if interrupt else 7)
                pid = int((root / "pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_startup_failure_stops_descendants_and_propagates_exit(self):
        self.exercise(False)

    def test_ctrl_c_stops_descendants_and_returns_130(self):
        self.exercise(True)


if __name__ == "__main__":
    unittest.main()
