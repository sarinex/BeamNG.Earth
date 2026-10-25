"""Optional dependency installation and Windows/Linux packaging command contracts."""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'server'))
import install


class Dependencies(unittest.TestCase):
    def test_legacy_does_not_run_pip(self):
        with mock.patch.object(install.subprocess, 'run') as run:
            self.assertTrue(install.setup_roadgeom(backend='legacy'))
        run.assert_not_called()

    def test_optional_failure_does_not_break_legacy_or_stamp_success(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, 'VENV', tmp):
            with mock.patch.object(install.subprocess, 'run', return_value=mock.Mock(returncode=1)):
                self.assertFalse(install.setup_roadgeom())
            self.assertFalse(Path(tmp, '.roadgeom-requirements.sha256').exists())

    def test_uses_existing_python_and_independent_stamp(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install, 'VENV', tmp):
            with mock.patch.object(install.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                self.assertTrue(install.setup_roadgeom())
                self.assertEqual(run.call_args_list[0].args[0][0], install.venv_python())
                self.assertEqual(run.call_args_list[0].args[0][-1], install.ROADGEOM_REQUIREMENTS)
                self.assertTrue(Path(tmp, '.roadgeom-requirements.sha256').exists())
                self.assertFalse(Path(tmp, '.requirements.sha256').exists())
                self.assertEqual(run.call_count, 2)
                self.assertTrue(install.setup_roadgeom())
                self.assertEqual(run.call_count, 2)

    def test_python_310_syntax_and_conditional_requirements(self):
        for source in (ROOT / 'server' / 'roadgeom_ext').glob('*.py'):
            ast.parse(source.read_text(encoding='utf-8'), feature_version=(3, 10))
        req = Path(install.ROADGEOM_REQUIREMENTS).read_text()
        self.assertIn('shapely==2.1.2; python_version < "3.11"', req)
        self.assertIn('shapely==2.2.0; python_version >= "3.11"', req)
        self.assertIn('mapbox-earcut==2.1.0', req)
        self.assertNotIn('shapely', Path(install.REQUIREMENTS).read_text().lower())

    def test_source_zip_contains_extension_and_not_environment(self):
        spec = importlib.util.spec_from_file_location('package_roadgeom_test', ROOT / 'tools' / 'package.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        files = module.collect()
        self.assertIn('server/requirements-roadgeom-ext.txt', files)
        self.assertIn('server/roadgeom_ext/geos_backend.py', files)
        self.assertFalse(any('.venv' in name for name in files))

    def test_both_packaging_platforms_collect_native_libraries(self):
        spec = importlib.util.spec_from_file_location('build_roadgeom_test', ROOT / 'tools' / 'build_app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for windows in (True, False):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(module, 'IS_WIN', windows), mock.patch.object(module, 'version_files', return_value=('v','m')):
                    with mock.patch.object(module.subprocess, 'run') as run:
                        module.pyinstaller(tmp, 'icon', onefile=windows)
                cmd = run.call_args.args[0]
                self.assertIn('roadgeom_ext', cmd)
                for package in ('shapely', 'mapbox_earcut'):
                    at = cmd.index(package)
                    self.assertEqual(cmd[at-1], '--collect-all')
        for workflow in [ROOT / '.github' / 'workflows' / 'release.yml']:
            self.assertEqual(workflow.read_text().count('-r server/requirements-roadgeom-ext.txt'), 2)


if __name__ == '__main__':
    unittest.main()
