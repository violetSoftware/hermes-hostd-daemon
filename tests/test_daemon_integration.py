"""Real loopback HTTP + controlling PTY tests; never reads runtime config.

HOSTD_TEST_COMMAND selects an installed command (absolute path). Default is
legacy checkout launcher. HOSTD_TEST_PYTHON selects a disposable interpreter.
All fixtures use TMPDIR; answers are automated, not independent human approval.
"""
import concurrent.futures
import fcntl
import http.client
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def controlling_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Daemon:
    def __init__(self, approval_timeout=2):
        self.tmp = tempfile.TemporaryDirectory(prefix='hostd-test-', dir=os.environ['TMPDIR'])
        self.root = Path(self.tmp.name)
        self.marker = self.root / 'executed'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        config = self.root / 'fixture.json'
        config.write_text(json.dumps({
            'transport': {'type': 'http', 'host': '127.0.0.1', 'port': self.port, 'auth_token': 'synthetic-local-test'},
            'approval': {'type': 'cli', 'timeout': approval_timeout},
            'executor': {'allowlist': ['/usr/bin/printf', '/usr/bin/false', '/usr/bin/touch', '/usr/bin/sleep']},
        }))
        (self.root / 'home').mkdir()
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root / 'home'),
               'TMPDIR': str(self.root), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        command = shlex.split(os.environ.get('HOSTD_TEST_COMMAND', ''))
        if not command:
            command = [os.environ.get('HOSTD_TEST_PYTHON', sys.executable), '-B', str(ROOT / 'hostd.py')]
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(command + ['--config', str(config)], cwd=self.root,
                                        env=env, stdin=slave, stdout=slave, stderr=slave,
                                        preexec_fn=controlling_terminal)
        os.close(slave)
        self.transcript = bytearray()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            self.until(b'listening')
        except BaseException:
            self.close()
            raise

    def until(self, marker, seconds=5):
        start = len(self.transcript)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    self.transcript.extend(os.read(self.master, 65536))
                except OSError:
                    break
                if marker in self.transcript[start:]:
                    return
            if self.process.poll() is not None:
                break
        raise AssertionError(f'Missing {marker!r}: {self.transcript.decode(errors="replace")}')

    def post(self, payload, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=6)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer synthetic-local-test'
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        try:
            connection.request('POST', '/execute', raw, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def request(self, command, answer=b'y\n', timeout=3, **fields):
        pending = self.pool.submit(self.post, {'id': 'integration', 'description': 'isolated test',
                                              'command': command, 'timeout': timeout, **fields})
        self.until(b'Decision [y/N]:')
        if answer is not None:
            os.write(self.master, answer)
        status, raw = pending.result(timeout=6)
        if status != 200:
            raise AssertionError((status, raw))
        return json.loads(raw)

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(3)
        os.close(self.master)
        self.pool.shutdown(wait=True)
        self.tmp.cleanup()


class RealDaemonTests(unittest.TestCase):
    def test_persistent_intent_does_not_change_finite_unlimited_or_denied_execution(self):
        # All commands bounded and foreground; no persistent service launched.
        for marker in (False, True):
            result = self.daemon.request(['/usr/bin/sleep', '2'], timeout=1, persistent_intent=marker)
            self.assertTrue(result['approved'])
            self.assertEqual(result['error'], 'command timed out after 1s')
            self.assertIsNone(result['exit_code'])
            result = self.daemon.request(['/usr/bin/printf', 'completed'], timeout=0,
                                         persistent_intent=marker)
            self.assertEqual(result['stdout'], 'completed')
            self.assertEqual(result['exit_code'], 0)
            denied = self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'n\n',
                                         persistent_intent=marker)
            self.assertFalse(denied['approved'])
            self.assertFalse(self.daemon.marker.exists())

    def test_malformed_persistent_intent_rejected_before_prompt(self):
        for marker in (None, 0, 1, 'true', [], {}):
            status, _ = self.daemon.post({'description': 'synthetic', 'command': ['/usr/bin/false'],
                                           'persistent_intent': marker})
            self.assertEqual(status, 400)
        self.assertNotIn(b'Decision [y/N]:', self.daemon.transcript)

    def setUp(self):
        self.daemon = Daemon()
        self.addCleanup(self.daemon.close)

    def test_approved_literal_argv(self):
        result = self.daemon.request(['/usr/bin/printf', '%s', 'literal; no shell'])
        self.assertTrue(result['approved'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['stdout'], 'literal; no shell')
        self.assertEqual(result['id'], 'integration')

    def test_denied_command_does_not_execute(self):
        result = self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'n\n')
        self.assertFalse(result['approved'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['error'], 'request denied by operator')
        self.assertFalse(self.daemon.marker.exists())

    def test_separate_plugin_client_real_round_trip(self):
        import importlib.util
        client_path = os.environ.get('HOSTD_PLUGIN_CLIENT')
        if not client_path:
            self.skipTest('set HOSTD_PLUGIN_CLIENT to the separate release client.py')
        spec = importlib.util.spec_from_file_location('isolated_release_client', client_path)
        assert spec is not None and spec.loader is not None
        client = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = client
        spec.loader.exec_module(client)
        transport = client.HttpClient(f'http://127.0.0.1:{self.daemon.port}/execute',
                                      timeout=6, auth_token='synthetic-local-test')
        for command, answer, approved, code, output in (
            (['/usr/bin/printf', 'plugin round trip'], b'y\n', True, 0, 'plugin round trip'),
            (['/usr/bin/touch', str(self.daemon.marker)], b'n\n', False, None, ''),
            (['/usr/bin/false'], b'y\n', True, 1, ''),
        ):
            request = client.CommandRequest(description='plugin integration', command=command, timeout=3)
            pending = self.daemon.pool.submit(transport.send, request.to_json())
            self.daemon.until(b'Decision [y/N]:')
            os.write(self.daemon.master, answer)
            result = client.CommandResult.from_json(pending.result(timeout=6))
            self.assertEqual(result.id, request.id)
            self.assertEqual(result.approved, approved)
            self.assertEqual(result.exit_code, code)
            self.assertEqual(result.stdout, output)
        self.assertFalse(self.daemon.marker.exists())

    def test_command_failure(self):
        result = self.daemon.request(['/usr/bin/false'])
        self.assertTrue(result['approved'])
        self.assertEqual(result['exit_code'], 1)

    def test_execution_timeout(self):
        result = self.daemon.request(['/usr/bin/sleep', '2'], timeout=1)
        self.assertTrue(result['approved'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['error'], 'command timed out after 1s')

    def test_missing_auth_rejected_before_approval(self):
        status, _ = self.daemon.post({'command': ['/usr/bin/false']}, token=False)
        self.assertEqual(status, 401)
        self.assertNotIn(b'Decision [y/N]:', self.daemon.transcript)

    def test_malformed_payloads_return_400_and_daemon_survives(self):
        for payload in (b'{', {}, {'description': 'invalid', 'command': []}, [], None):
            with self.subTest(payload=payload):
                status, _ = self.daemon.post(payload)
                self.assertEqual(status, 400)
        self.assertNotIn(b'Decision [y/N]:', self.daemon.transcript)
        self.assertEqual(self.daemon.request(['/usr/bin/printf', 'alive'])['stdout'], 'alive')

    def test_canonical_partial_eof_cannot_block_past_approval_deadline(self):
        pending = self.daemon.pool.submit(self.daemon.post, {
            'id': 'partial-eof', 'description': 'deadline regression',
            'command': ['/usr/bin/touch', str(self.daemon.marker)], 'timeout': 3,
        })
        self.daemon.until(b'Decision [y/N]:')
        os.write(self.daemon.master, b'y\x04')
        try:
            status, raw = pending.result(timeout=3)
        except concurrent.futures.TimeoutError:
            self.fail('canonical y + Ctrl-D blocked beyond the 2s approval deadline')
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertFalse(result['approved'])
        self.assertEqual(result['error'], 'request denied by operator')
        os.write(self.daemon.master, b'\n')  # late input cannot change the decision
        self.assertFalse(self.daemon.marker.exists())
        self.assertFalse(self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'\n')['approved'])
        self.assertFalse(self.daemon.marker.exists())

    def test_partial_answer_is_discarded_before_next_request(self):
        first = self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'y')
        self.assertFalse(first['approved'])
        self.assertFalse(self.daemon.marker.exists())
        second = self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'\n')
        self.assertFalse(second['approved'], 'empty second answer reused stale y from timed-out request')
        self.assertEqual(second['error'], 'request denied by operator')
        self.assertFalse(self.daemon.marker.exists())
        self.assertEqual(self.daemon.request(['/usr/bin/printf', 'fresh answer'])['stdout'], 'fresh answer')

    def test_canonical_operator_editing_and_default_denial(self):
        attributes = termios.tcgetattr(self.daemon.master)
        self.assertTrue(attributes[3] & termios.ICANON)
        self.assertTrue(attributes[3] & termios.ECHO)
        erase = attributes[6][termios.VERASE]
        for answer, approved in ((b'n' + erase + b'Y\n', True),
                                 (b' YES \n', True), (b'\n', False),
                                 (b'unknown\n', False), (b'\x04', False)):
            with self.subTest(answer=answer):
                result = self.daemon.request(['/usr/bin/printf', 'edited'], answer)
                self.assertEqual(result['approved'], approved)
                self.assertEqual(termios.tcgetattr(self.daemon.master), attributes)

    def test_idle_and_extra_input_cannot_answer_next_prompt(self):
        self.assertTrue(self.daemon.request(['/usr/bin/printf', 'first'], b'y\ny\n')['approved'])
        os.write(self.daemon.master, b'y')
        self.assertFalse(self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], b'\n')['approved'])
        self.assertFalse(self.daemon.marker.exists())

    def test_unlimited_approval_still_requires_a_complete_fresh_line(self):
        daemon = Daemon(approval_timeout=0)
        self.addCleanup(daemon.close)
        pending = daemon.pool.submit(daemon.post, {
            'id': 'unlimited', 'description': 'unlimited approval',
            'command': ['/usr/bin/printf', 'unlimited'], 'timeout': 3,
        })
        daemon.until(b'Decision [y/N]:')
        os.write(daemon.master, b'y\x04')
        time.sleep(0.2)
        self.assertFalse(pending.done())
        os.write(daemon.master, b'\n')
        status, raw = pending.result(timeout=3)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['stdout'], 'unlimited')
        self.assertTrue(json.loads(raw)['approved'])

    def test_approval_timeout_then_next_request_can_be_approved(self):
        result = self.daemon.request(['/usr/bin/touch', str(self.daemon.marker)], None)
        self.assertFalse(result['approved'])
        self.assertEqual(result['error'], 'request denied by operator')
        self.assertFalse(self.daemon.marker.exists())
        result = self.daemon.request(['/usr/bin/printf', 'after timeout'])
        self.assertTrue(result['approved'])
        self.assertEqual(result['stdout'], 'after timeout')


if __name__ == '__main__':
    unittest.main()
