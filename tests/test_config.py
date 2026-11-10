"""Configuration selection uses synthetic files and injected discovery only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd import hostd


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fallback = self.root / 'discovery.json'
        self.fallback.write_text('{"transport":{"type":"http","insecure_no_auth":true}}')
        self.discovery = patch.object(hostd, '_DEFAULT_CONFIG_PATHS', [str(self.fallback)])
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def test_explicit_missing_never_uses_valid_discovery(self):
        literal = str(self.root / './missing.json')
        with self.assertRaisesRegex(ValueError, 'cannot read configuration'):
            hostd._load_config(literal)


    def test_explicit_invalid_paths_and_io_never_discover(self):
        for path in ('', ' ', 'bad\x00path', str(self.root), str(self.root / 'missing')):
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                hostd._load_config(path)
        unreadable = self.root / 'unreadable'
        unreadable.write_text('{}')
        # Deterministic even when tests run as root. No real permission boundary assumed.
        with patch.object(hostd.os, 'open', side_effect=PermissionError('synthetic-secret')):
            with self.assertRaisesRegex(ValueError, 'cannot read configuration'):
                hostd._load_config(str(unreadable))

    def test_parse_and_supported_policy_validation(self):
        invalid = ('', 'null', 'false', '[]', '42', '"synthetic-secret"', '{bad',
                   '{"executor":{"allowlist":"echo"}}',
                   '{"executor":{"allowlist":[1]}}',
                   '{"executor":null}', '{"transport":[]}',
                   '{"transport":{"type":"HTTP"}}',
                   '{"approval":{"type":" cli"}}',
                   '{"transport":{"port":true}}',
                   '{"transport":{"port":"9998oops"}}',
                   '{"transport":{"receive_timeout":0}}',
                   '{"approval":{"max_pending":0}}',
                   '{"executor":{"logs":{"budget_bytes":1}}}',
                   '{"executor":{"logs":{"tail_bytes":0}}}',
                   '{"transport":{"insecure_no_auth":"true"}}',
                   '{"transport":{"auth_token_file":""}}',
                   '{"log_level":23}')
        path = self.root / 'explicit.json'
        for content in invalid:
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                hostd._load_config(str(path))
        path.write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            hostd._load_config(str(path))

    def test_valid_explicit_and_discovery_are_distinct(self):
        path = self.root / 'explicit.json'
        cfg = {'transport': {'type': 'vsock', 'insecure_no_auth': True},
               'executor': {'allowlist': []}, 'extension': {'future': True}}
        path.write_text(json.dumps(cfg))
        self.assertEqual(hostd._load_config(str(path)), cfg)
        self.assertEqual(hostd._load_config(None)['transport']['type'], 'http')
        with patch.object(hostd, '_DEFAULT_CONFIG_PATHS', [str(self.root / 'absent')]):
            self.assertEqual(hostd._load_config(None), {})


    def test_cli_errors_are_bounded_and_do_not_echo_document_or_exception(self):
        path = self.root / 'bad.json'
        cases = ('{synthetic-secret', '{"transport":{"auth_token":"synthetic-secret", "insecure_no_auth":true}}',
                 '{"transport":{"type":"synthetic-secret"}}', '{"log_level": ["synthetic-secret"]}')
        for content in cases:
            path.write_text(content)
            output = io.StringIO()
            with self.subTest(content=content), patch.object(sys, 'argv', ['hostd', '--config', str(path)]), \
                    contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as exit:
                    hostd.main()
                self.assertEqual(exit.exception.code, 1)
            text = output.getvalue()
            self.assertIn('hostd: configuration', text)
            self.assertIn(str(path), text)
            self.assertNotIn('synthetic-secret', text)
            self.assertNotIn('Traceback', text)
            self.assertLess(len(text), 900)

    def test_cli_auth_defaults_and_valid_selection(self):
        path = self.root / 'explicit.json'
        for cfg in ({}, {'transport': {'insecure_no_auth': 'true'}},
                    {'transport': {'auth_token_file': '', 'insecure_no_auth': True}}):
            path.write_text(json.dumps(cfg))
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', ['hostd', '--config', str(path)]), \
                    contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as exit:
                    hostd.main()
                self.assertEqual(exit.exception.code, 1)
            self.assertIn('hostd: configuration', output.getvalue())
        cfg = {'transport': {'type': 'http', 'insecure_no_auth': True}}
        path.write_text(json.dumps(cfg))
        seen = []
        async def inert(config):
            seen.append(config)
        with patch.object(sys, 'argv', ['hostd', '--config', str(path)]), patch.object(hostd, '_async_main', inert):
            hostd.main()
        self.assertEqual(seen, [cfg])


    def test_fifo_rejected_without_blocking_and_literal_path_not_normalized(self):
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            hostd._load_config(str(fifo))
        # Collapsing missing/.. would incorrectly select a real valid file.
        valid = self.root / 'valid.json'
        valid.write_text('{"transport":{"insecure_no_auth":true}}')
        with self.assertRaises(ValueError):
            hostd._load_config(str(self.root) + '/missing/../valid.json')
        for name in ('HTTP', ' http', 'http ', 'Http'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                hostd._build_transport({'transport': {'type': name, 'insecure_no_auth': True}})

    def test_cli_log_command_rejects_malformed_config_without_disclosure(self):
        path = self.root / 'bad.json'
        path.write_text('{synthetic-secret')
        output = io.StringIO()
        with patch.object(sys, 'argv', ['hostd', '--config', str(path), 'logs', 'cleanup', '--all']), \
                contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit:
                hostd.main()
        self.assertEqual(exit.exception.code, 1)
        self.assertIn('hostd: configuration', output.getvalue())
        self.assertNotIn('synthetic-secret', output.getvalue())


    def test_real_entrypoint_failure_status_and_diagnostics(self):
        import shlex
        import subprocess
        command = shlex.split(os.environ.get('HOSTD_TEST_COMMAND', ''))
        env = {**os.environ, 'HOSTD_AUTH_TOKEN': '', 'HOSTD_AUTH_TOKEN_FILE': '',
               'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state')}
        if not command:
            command = [sys.executable, '-B', '-m', 'hermes_hostd']
            env['PYTHONPATH'] = str(Path(hostd.__file__).resolve().parents[1])
        # If any invalid selection falls through to cwd it could otherwise serve.
        (self.root / 'config.yaml').write_text(self.fallback.read_text())
        path = self.root / 'bad.json'
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        cases = [(str(self.root / 'missing'), None), ('', None),
                 (str(self.root), None), (str(fifo), None),
                 (str(path), '{synthetic-secret'),
                 (str(path), '{"executor":{"allowlist":"synthetic-secret"}}'),
                 (str(path), '{}')] + [(str(path), json.dumps({
                     'transport': {'type': 'http', 'insecure_no_auth': True},
                     'approval': {'type': 'ntfy', 'base_url': endpoint,
                                  'request_topic': 'fixture-request', 'response_topic': 'fixture-response'}}))
                     for endpoint in ('not-a-url', 'ftp://example.invalid', 'https://',
                                      'https://example.invalid:bad', 'https://example.invalid#synthetic-secret')]
        for literal, content in cases + [
            (str(path), '{"transport":{"auth_token":"synthetic-secret","insecure_no_auth":true}}')]:
            if content is not None:
                path.write_text(content)
            result = subprocess.run(command + ['--config', literal], cwd=self.root, env=env,
                                    capture_output=True, text=True, timeout=5)
            with self.subTest(literal=literal, content=content):
                self.assertEqual(result.returncode, 1)
                self.assertIn('hostd: configuration', result.stderr)
                self.assertNotIn('Traceback', result.stdout + result.stderr)
                self.assertNotIn('synthetic-secret', result.stdout + result.stderr)
                self.assertLess(len(result.stderr), 900)


    def test_ntfy_invalid_endpoint_fails_before_components_or_discovery(self):
        path = self.root / 'explicit.json'
        invalid = ('not-a-url', '//example.invalid', 'ftp://example.invalid',
                   'https://', 'https:///missing-host', 'https://:443',
                   'https://example.invalid:bad', 'https://example.invalid:65536',
                   'https://example.invalid:', 'https://[broken',
                   'https://example.invalid?synthetic-secret',
                   'https://example.invalid#synthetic-secret',
                   'https://user:synthetic-secret@example.invalid',
                   ' https://example.invalid', 'HTTPS://example.invalid',
                   'https://exa\\mple.invalid', 'https://example.invalid/\x7f',
                   'https://exa%mple.invalid', 'https://example.invalid/bad%zz')
        for endpoint in invalid:
            cfg = {'transport': {'type': 'http', 'insecure_no_auth': True},
                   'approval': {'type': 'ntfy', 'base_url': endpoint,
                                'request_topic': 'fixture-request', 'response_topic': 'fixture-response'},
                   'executor': {'logs': {'directory': str(self.root / 'logs')}}}
            path.write_text(json.dumps(cfg))
            output = io.StringIO()
            with self.subTest(endpoint=endpoint), patch.object(sys, 'argv', ['hostd', '--config', str(path)]), \
                    patch.object(hostd, '_build_transport') as build, \
                    contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    hostd.main()
                self.assertEqual(result.exception.code, 1)
                build.assert_not_called()
            self.assertIn('hostd: configuration', output.getvalue())
            self.assertNotIn('synthetic-secret', output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())
            self.assertLess(len(output.getvalue()), 900)

    def test_ntfy_valid_http_https_endpoints_reach_inert_start_unchanged(self):
        from hermes_hostd.transport.http import HttpServer
        path = self.root / 'explicit.json'
        for endpoint in ('http://localhost:8080', 'https://example.invalid',
                         'https://example.invalid/prefix/', 'http://127.0.0.1:8080',
                         'https://[::1]:8443/prefix', 'https://example.invalid/a%20b'):
            cfg = {'transport': {'type': 'http', 'auth_token': 'fixture-only'},
                   'approval': {'type': 'ntfy', 'base_url': endpoint,
                                'request_topic': 'fixture-request', 'response_topic': 'fixture-response'},
                   'executor': {'logs': {'directory': str(self.root / 'logs')}}}
            path.write_text(json.dumps(cfg))
            reached = []
            async def start(server, handler):
                reached.append(True)
            with self.subTest(endpoint=endpoint), patch.object(sys, 'argv', ['hostd', '--config', str(path)]), \
                    patch.object(HttpServer, 'start', start), patch.object(hostd, '_install_signal_handlers'):
                loaded = hostd._load_config(str(path))
                self.assertEqual(loaded, cfg)
                hostd.main()
            self.assertEqual(reached, [True])

    def test_auth_migration_config_sources_and_extension_factories(self):
        token = self.root / 'requester.token'
        token.write_text('synthetic-file-token\n')
        token.chmod(0o600)
        path = self.root / 'explicit.json'
        for kind in ('http', 'vsock'):
            for source in ({'auth_token_file': str(token)}, {'auth_token': 'synthetic-inline'},
                           {'auth_token_env': 'FIXTURE_REQUESTER'}, {'insecure_no_auth': True}):
                cfg = {'transport': {'type': kind, **source}}
                path.write_text(json.dumps(cfg))
                with patch.dict(os.environ, {'FIXTURE_REQUESTER': 'synthetic-env'}):
                    loaded = hostd._load_config(str(path))
                    self.assertIsNotNone(hostd._build_transport(loaded))
        # Invalid first discoverable config is not skipped for a later valid one.
        bad = self.root / 'first.json'
        bad.write_text('null')
        with patch.object(hostd, '_DEFAULT_CONFIG_PATHS', [str(bad), str(self.fallback)]):
            with self.assertRaises(ValueError):
                hostd._load_config(None)
        with patch.dict(hostd._transport_factories, {'fixture-extension': lambda block: block}):
            cfg = {'transport': {'type': 'fixture-extension', 'custom': 123}}
            path.write_text(json.dumps(cfg))
            self.assertEqual(hostd._build_transport(hostd._load_config(str(path))), cfg['transport'])
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            hostd._config_error('a\n\x1b' + 'x' * 2000, 'invalid configuration')
        self.assertNotIn('\x1b', output.getvalue())
        self.assertLess(len(output.getvalue()), 500)


if __name__ == '__main__':
    unittest.main()
