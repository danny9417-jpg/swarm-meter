"""Portability regressions use invented homes, relocated sources and no user data."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import config


class PathDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="swarm config ")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.home = self.root / "someone else"
        self.app = self.root / "any checkout name"
        self.app.mkdir()

    def data(self, **kwargs):
        return config.data_directory(app_dir=self.app, home=self.home, env=kwargs)

    def test_fresh_checkout_uses_user_state_not_source(self):
        self.assertEqual(self.data(), self.home / ".local/state/swarm-meter")
        self.assertFalse((self.home / ".local/state").exists())

    def test_xdg_overrides_are_absolute_and_relative_values_ignored(self):
        self.assertEqual(
            self.data(XDG_STATE_HOME="/mnt/user state"),
            Path("/mnt/user state/swarm-meter"),
        )
        self.assertEqual(
            self.data(XDG_STATE_HOME="relative"), self.home / ".local/state/swarm-meter"
        )
        self.assertEqual(
            self.data(XDG_STATE_HOME=""), self.home / ".local/state/swarm-meter"
        )
        self.assertEqual(
            self.data(SWARM_METER_DATA_DIR="/opt/test state"), Path("/opt/test state")
        )

    def test_legacy_history_stays_in_place_with_same_lock_directory(self):
        legacy = self.home / ".local/share/swarm-meter"
        legacy.mkdir(parents=True)
        (legacy / "history.db").touch()
        self.assertEqual(self.data(), legacy)
        self.assertEqual(self.data(SWARM_METER_DATA_DIR="/explicit"), Path("/explicit"))
        (self.app / "state.json").write_text("{}")
        self.assertEqual(self.data(), self.app)
        self.assertEqual(
            config.data_directory(
                "/cli", app_dir=self.app, env={"SWARM_METER_DATA_DIR": "/env"}
            ),
            Path("/cli"),
        )

    def test_core_follows_relocated_checkout_and_overrides(self):
        root, binary = config.core_paths(app_dir=self.app, env={})
        self.assertEqual(root, self.app / "core")
        self.assertEqual(binary, root / ".lake/build/bin/core")
        root, binary = config.core_paths(
            app_dir=self.app, env={"SWARM_METER_CORE_DIR": "/custom core"}
        )
        self.assertEqual(binary, Path("/custom core/.lake/build/bin/core"))
        _, binary = config.core_paths(
            "/explicit binary", app_dir=self.app, env={"SWARM_METER_CORE": "/ignored"}
        )
        self.assertEqual(binary, Path("/explicit binary"))

    def test_interpreter_is_local_or_current_and_invalid_override_is_clear(self):
        self.assertEqual(
            config.python_runtime(app_dir=self.app, env={}), Path(sys.executable)
        )
        python = self.app / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.assertEqual(config.python_runtime(app_dir=self.app, env={}), python)
        self.assertEqual(
            config.python_runtime(
                app_dir=self.app, env={"SWARM_METER_PYTHON": sys.executable}
            ),
            Path(sys.executable),
        )
        with self.assertRaisesRegex(RuntimeError, "executable Python"):
            config.python_runtime(
                app_dir=self.app, env={"SWARM_METER_PYTHON": "/missing/python"}
            )

    def test_private_native_libraries_follow_venv_and_cpu_abi(self):
        prefix = self.app / ".venv"
        native = prefix / "native/usr/lib/aarch64-linux-gnu"
        native.mkdir(parents=True)
        with patch.object(
            config.sysconfig, "get_config_var", return_value="aarch64-linux-gnu"
        ):
            env = config.runtime_environment(
                prefix / "bin/python", env={"LD_LIBRARY_PATH": "/prior"}
            )
            self.assertEqual(env["LD_LIBRARY_PATH"].split(":")[0], str(native))
            self.assertTrue(env["LD_LIBRARY_PATH"].endswith(":/prior"))
            self.assertEqual(
                config.runtime_environment(prefix / "bin/python", env=env), env
            )

    def test_relocated_entry_and_shell_work_with_spaces_and_without_qt(self):
        # --version deliberately needs neither Qt, an installed core nor writes.
        for name in ("config.py", "swarm_meter.py", "run.sh"):
            shutil.copy2(Path(__file__).parent / name, self.app / name)
        env = dict(os.environ)
        for key in ("SWARM_METER_PYTHON", "SWARM_METER_REEXEC", "SWARM_METER_DATA_DIR"):
            env.pop(key, None)
        for command in (
            [sys.executable, str(self.app / "swarm_meter.py"), "--version"],
            ["bash", str(self.app / "run.sh"), "--version"],
        ):
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(config.VERSION, result.stdout)
        self.assertEqual(
            {p.name for p in self.app.iterdir()} - {"__pycache__"},
            {"config.py", "swarm_meter.py", "run.sh"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
