"""Installer regressions use scratch folders and fakes, never real installations."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import install


class LauncherTests(unittest.TestCase):
    def test_exec_preserves_spaces_and_escapes_interpretable_characters(self):
        # Desktop Entry has two escape layers; do not substitute shell quoting.
        text = install.desktop_entry(Path('/tmp/A person/50% $stuff "quoted" \\'))
        line = next(s for s in text.splitlines() if s.startswith('Exec='))
        self.assertIn('A person', line)
        self.assertIn('50%%', line)
        self.assertIn('\\\\$', line)
        self.assertIn('\\\\"quoted\\\\"', line)
        self.assertIn('\\\\\\\\', line)
        self.assertTrue(line.startswith('Exec="'))
        self.assertTrue(line.endswith('/run.sh"'))

    def test_control_characters_fail_and_equals_paths_remain_arguments(self):
        with self.assertRaises(ValueError):
            install.desktop_entry(Path('/tmp/unsafe\npath'))
        self.assertIn('"/tmp/has=equals/run.sh"', install.desktop_entry(Path('/tmp/has=equals')))

    def test_source_allowlist_excludes_personal_data_and_prebuilt_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ('main.py', 'state.json', 'history.db', 'widget.log',
                             'core/Main.lean', 'core/lean-toolchain',
                             'core/.lake/build/bin/core'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            self.assertEqual([str(p.relative_to(root)) for p in install.source_files(root)],
                             ['core/Main.lean', 'core/lean-toolchain', 'main.py'])


class SetupSafetyTests(unittest.TestCase):
    def test_held_app_lock_refuses_upgrade_without_truncating_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.lock'
            path.write_text('12345\n')
            with path.open('a+') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, 'Close its window'):
                    with install.held_lock(path):
                        self.fail('A live lock cannot be acquired')
            self.assertEqual(path.read_text(), '12345\n')

    def test_existing_venv_is_never_changed_for_different_reuse_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / '.venv'
            target.mkdir()
            marker = target / 'kept'
            marker.write_text('original')
            with mock.patch.object(install, 'validate_python') as validate:
                with self.assertRaisesRegex(RuntimeError, 'will not be overwritten'):
                    install.prepare_venv({'venv': str(target), 'reuse_venv': str(root / 'other')})
            validate.assert_not_called()
            self.assertEqual(marker.read_text(), 'original')

    def test_failed_venv_validation_does_not_create_a_reuse_link(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.venv'
            with mock.patch.object(install, 'validate_python', side_effect=RuntimeError('incompatible')):
                with self.assertRaisesRegex(RuntimeError, 'incompatible'):
                    install.prepare_venv({'venv': str(target), 'reuse_venv': str(Path(directory) / 'provided')})
            self.assertFalse(target.exists())
            self.assertFalse(target.is_symlink())

    def test_validation_failure_leaves_installed_source_and_launcher_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'application'
            app.mkdir()
            (app / 'main.py').write_text('previous working revision')
            menu = root / 'menu.desktop'
            menu.write_text('previous launcher')
            record = {'install_dir': str(app), 'runtime_data': str(root / 'data'),
                      'venv': str(app / '.venv'), 'reuse_venv': None,
                      'menu_entry': str(menu), 'desktop_entry': None, 'lake': '/fake/lake'}
            with mock.patch.object(install, 'prepare_venv', return_value=Path('/fake/python')):
                with mock.patch.object(install, 'validate_stage', side_effect=RuntimeError('proof failed')):
                    with self.assertRaisesRegex(RuntimeError, 'proof failed'):
                        install.install(record)
            self.assertEqual((app / 'main.py').read_text(), 'previous working revision')
            self.assertEqual(menu.read_text(), 'previous launcher')
            self.assertFalse((app / 'install-backups').exists())

    def test_installed_assets_follow_custom_prefix_and_data_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'A different user application'
            app.mkdir()
            state = app / 'state.json'
            state.write_text('{"collapsed":true}')
            menu = root / 'applications/swarm-meter.desktop'
            record = {'install_dir': str(app), 'runtime_data': str(app),
                      'venv': str(app / '.venv'), 'reuse_venv': None,
                      'menu_entry': str(menu), 'desktop_entry': None, 'lake': '/fake/lake'}

            def fake_stage(stage, *_):
                binary = stage / 'core/.lake/build/bin/core'
                binary.parent.mkdir(parents=True)
                binary.write_text('validated fake binary')
                return 'fake proof receipt'

            with mock.patch.object(install, 'prepare_venv', return_value=Path('/fake/python')):
                with mock.patch.object(install, 'validate_stage', side_effect=fake_stage):
                    result = install.install(record)
            self.assertEqual(state.read_text(), '{"collapsed":true}')
            self.assertIn(install.desktop_argument(app / 'run.sh'), menu.read_text())
            self.assertEqual((app / 'run.sh').stat().st_mode & 0o777, 0o755)
            self.assertEqual(json.loads(Path(result['manifest']).read_text())['status'], 'installed')
            self.assertFalse(result['automatic_start'])

    def test_copy_failure_restores_previous_revision_and_retains_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'app'
            app.mkdir()
            (app / 'main.py').write_text('previous source')
            (app / 'state.json').write_text('keep history/settings')
            record = {'install_dir': str(app), 'runtime_data': str(app),
                      'venv': str(app / '.venv'), 'reuse_venv': None,
                      'menu_entry': str(root / 'menu.desktop'), 'desktop_entry': None, 'lake': '/fake/lake'}
            source = root / 'source'
            source.mkdir()
            (source / 'main.py').write_text('new source')
            (source / 'added.py').write_text('new module')

            def fake_stage(stage, *_):
                binary = stage / 'core/.lake/build/bin/core'
                binary.parent.mkdir(parents=True)
                binary.write_text('fake binary')
                return 'fake audit'

            original_copy = install.atomic_copy
            calls = 0

            def fail_third_copy(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError('injected filesystem failure')
                return original_copy(*args, **kwargs)

            with mock.patch.object(install, 'SOURCE', source), \
                    mock.patch.object(install, 'source_files', return_value=[source/'main.py', source/'added.py']), \
                    mock.patch.object(install, 'prepare_venv', return_value=Path('/fake/python')), \
                    mock.patch.object(install, 'validate_stage', side_effect=fake_stage), \
                    mock.patch.object(install, 'atomic_copy', side_effect=fail_third_copy):
                with self.assertRaisesRegex(OSError, 'injected filesystem'):
                    install.install(record)
            self.assertEqual((app / 'main.py').read_text(), 'previous source')
            self.assertFalse((app / 'added.py').exists())
            self.assertEqual((app / 'state.json').read_text(), 'keep history/settings')
            manifests = list((app / 'install-backups').glob('*/manifest.json'))
            self.assertEqual(json.loads(manifests[0].read_text())['status'], 'rolled_back_after_error')
            self.assertTrue(list((app / 'install-backups').rglob('added.py')))


if __name__ == '__main__':
    unittest.main()
