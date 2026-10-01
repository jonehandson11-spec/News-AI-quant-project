"""Real OS lock release tests, without Git, credentials, or network requests."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.ft_sync import SyncError, _lock


ROOT = Path(__file__).resolve().parents[1]


class FTProcessLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def test_stale_file_does_not_block_and_file_is_preserved(self):
        lock = self.state / "prepare.lock"
        lock.write_text("an old process already exited\n", encoding="utf-8")
        identity = lock.stat().st_ino
        with _lock(self.state):
            with self.assertRaisesRegex(SyncError, "state_locked"):
                with _lock(self.state):
                    self.fail("Concurrent lock was granted")
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.stat().st_ino, identity)
        with _lock(self.state):
            pass

    def test_exception_releases_lock_without_removing_file(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with _lock(self.state):
                raise RuntimeError("fixture failure")
        self.assertTrue((self.state / "prepare.lock").is_file())
        with _lock(self.state):
            pass

    def test_forcibly_terminated_process_releases_os_lock(self):
        ready = self.state / "ready"
        code = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "from scripts.ft_sync import _lock\n"
            "with _lock(Path(sys.argv[1])):\n"
            "    Path(sys.argv[2]).write_text('locked', encoding='ascii')\n"
            "    time.sleep(60)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.state), str(ready)], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "Child did not acquire its process lock")
            with self.assertRaisesRegex(SyncError, "state_locked"):
                with _lock(self.state):
                    self.fail("A second process acquired the active lock")
            child.kill()
            child.communicate(timeout=10)
            with _lock(self.state):
                pass
            self.assertTrue((self.state / "prepare.lock").is_file())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
