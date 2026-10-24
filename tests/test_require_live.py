"""Require-live tests: lost credentials must fail loudly, not run silently in dry-run.
Run: python3 -m unittest discover -s tests"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "inbox_watcher.py")
ENSURE = os.path.join(ROOT, "ensure_watcher.sh")


class RequireLiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("WAKE_URL", "WAKE_AUTH", "AIW_REQUIRE_LIVE")}
        self.env.update({"AIW_INBOX": os.path.join(self.tmp.name, "inbox"),
                         "AIW_STATE_DIR": self.state, "AIW_LOOP_SECONDS": "1"})
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.tmp.cleanup()

    def run_py(self, *args, **extra):
        env = dict(self.env, **extra)
        return subprocess.run([sys.executable, PY, *args], env=env, capture_output=True, text=True, timeout=30)

    def start_bg(self, **extra):
        p = subprocess.Popen([sys.executable, PY], env=dict(self.env, **extra),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        for _ in range(50):
            if self.run_py("--status").returncode != 3:
                return p
            time.sleep(0.1)
        self.fail("watcher did not start")

    def test_refuses_to_start_without_credentials(self):
        r = self.run_py("--once", AIW_REQUIRE_LIVE="1")
        self.assertEqual(r.returncode, 4)
        self.assertIn("refusing to start", r.stderr)
        self.assertNotIn("example-not-a-real-token", r.stderr)

    def test_flag_is_same_as_env(self):
        r = self.run_py("--once", "--require-live")
        self.assertEqual(r.returncode, 4)

    def test_default_still_allows_dry_run(self):
        r = self.run_py("--once")
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(self.state, "watcher.mode")) as f:
            self.assertEqual(f.read().strip(), "DRY-RUN")

    def test_starts_live_with_credentials(self):
        r = self.run_py("--once", AIW_REQUIRE_LIVE="1",
                        WAKE_URL="https://example.invalid/wake", WAKE_AUTH="Bearer example-not-a-real-token")
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(self.state, "watcher.mode")) as f:
            self.assertEqual(f.read().strip(), "LIVE")

    def test_status_reports_running_watcher_mode_not_this_shell(self):
        self.start_bg()  # started without credentials -> DRY-RUN
        # The status shell HAS credentials; it must still report the running process's mode.
        r = self.run_py("--status", WAKE_URL="https://example.invalid/wake", WAKE_AUTH="Bearer x")
        self.assertEqual(r.returncode, 0)
        self.assertIn("mode: DRY-RUN (running watcher)", r.stdout)
        r = self.run_py("--status", AIW_REQUIRE_LIVE="1")
        self.assertEqual(r.returncode, 4)
        self.assertIn("ALERT", r.stdout)

    def test_status_ok_when_running_live(self):
        self.start_bg(WAKE_URL="https://example.invalid/wake", WAKE_AUTH="Bearer example-not-a-real-token")
        r = self.run_py("--status", AIW_REQUIRE_LIVE="1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("mode: LIVE (running watcher)", r.stdout)

    def test_ensure_script_refuses_without_credentials(self):
        env = dict(self.env, AIW_REQUIRE_LIVE="1")
        r = subprocess.run(["bash", ENSURE], env=env, capture_output=True, text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(r.returncode, 4)
        self.assertIn("not starting", r.stderr)
        self.assertNotEqual(self.run_py("--status").returncode, 0)


    def test_ensure_script_honours_require_live_flag(self):
        r = subprocess.run(["bash", ENSURE, "--require-live"], env=self.env, capture_output=True,
                           text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(r.returncode, 4)
        self.assertIn("not starting", r.stderr)

    def test_ensure_script_env_value_is_case_insensitive(self):
        env = dict(self.env, AIW_REQUIRE_LIVE=" TRUE ")
        r = subprocess.run(["bash", ENSURE], env=env, capture_output=True, text=True, timeout=30, cwd=self.tmp.name)
        self.assertEqual(r.returncode, 4)


if __name__ == "__main__":
    unittest.main()
