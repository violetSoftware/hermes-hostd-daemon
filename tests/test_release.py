"""Release contract: package import and externally registered factories."""
import importlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class PackageTests(unittest.TestCase):
    def test_namespaced_daemon_preserves_argv_execution(self):
        self.assertTrue((ROOT / 'pyproject.toml').is_file(), 'missing Python packaging')
        models = importlib.import_module('hermes_hostd.models')
        executor = importlib.import_module('hermes_hostd.executor')
        request = models.CommandRequest('package', 'literal argv', ['/usr/bin/printf', '%s', 'a; b'])
        result = executor.CommandExecutor().execute(request)
        self.assertEqual(result.stdout, 'a; b')
        self.assertEqual(result.exit_code, 0)

    def test_legacy_imports_share_package_types(self):
        sys.path.insert(0, str(ROOT))
        legacy = importlib.import_module('models')
        package = importlib.import_module('hermes_hostd.models')
        self.assertIs(legacy.CommandRequest, package.CommandRequest)
        self.assertIs(importlib.import_module('approval.cli').CliApprover,
                      importlib.import_module('hermes_hostd.approval.cli').CliApprover)
        self.assertIs(importlib.import_module('transport.http').HttpServer,
                      importlib.import_module('hermes_hostd.transport.http').HttpServer)
        legacy_daemon = importlib.import_module('hostd')
        package_daemon = importlib.import_module('hermes_hostd.hostd')
        self.assertIs(legacy_daemon, package_daemon)
        self.assertEqual(legacy_daemon._DEFAULT_CONFIG_PATHS[0], str(ROOT / 'config.yaml'))

    def test_portable_user_service_is_shipped(self):
        unit = ROOT / 'packaging/systemd/hermes-hostd.service'
        self.assertTrue(unit.is_file(), 'portable user service missing')
        text = unit.read_text()
        self.assertIn('WantedBy=default.target', text)
        self.assertIn('ExecStart=%h/.local/share/hermes-hostd/venv/bin/hostd', text)
        self.assertNotIn('/mnt/', text)
        self.assertNotIn('User=', text)
        self.assertNotIn('TTYPath=', text)

    def test_external_factories_receive_component_config(self):
        daemon = importlib.import_module('hermes_hostd.hostd')
        self.assertTrue(hasattr(daemon, 'register_transport'), 'transport registry missing')
        self.assertTrue(hasattr(daemon, 'register_approval'), 'approval registry missing')
        from hermes_hostd.transport.http import HttpServer
        from hermes_hostd.approval.cli import CliApprover
        daemon.register_transport('test-http', lambda cfg: HttpServer(port=cfg['port'], insecure_no_auth=True))
        daemon.register_approval('test-cli', lambda cfg: CliApprover(timeout=cfg['timeout']))
        self.assertEqual(daemon._build_transport({'transport': {'type': 'test-http', 'port': 1234}}).port, 1234)
        self.assertEqual(daemon._build_approval_channel({'approval': {'type': 'test-cli', 'timeout': 7}}).timeout, 7)
        with self.assertRaises(ValueError):
            daemon.register_transport('http', lambda cfg: None)
        with self.assertRaises(ValueError):
            daemon.register_approval('cli', lambda cfg: None)
        for name in ('', 'MixedCase'):
            with self.assertRaises(ValueError):
                daemon.register_transport(name, lambda cfg: None)
        with self.assertRaises(TypeError):
            daemon.register_approval('noncallable', None)
        with self.assertRaises(ValueError):
            daemon._build_transport({'transport': {'type': 'does-not-exist'}})
        with self.assertRaises(ValueError):
            daemon._build_approval_channel({'approval': {'type': 'does-not-exist'}})


if __name__ == '__main__':
    unittest.main()
