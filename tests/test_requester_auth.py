"""Synthetic requester credentials; no production sockets or settings."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.hostd import _build_transport
from hermes_hostd.transport.http import HttpServer
from hermes_hostd.transport.vsock import VsockServer


class ConfigurationTests(unittest.TestCase):
    def test_missing_credentials_fail_closed_for_both_constructors_and_config(self):
        with patch.dict(os.environ, {}, clear=True):
            for constructor in (HttpServer, VsockServer):
                with self.subTest(constructor=constructor.__name__):
                    with self.assertRaisesRegex(ValueError, 'requester token'):
                        constructor()
            for transport in ('http', 'vsock'):
                with self.assertRaisesRegex(ValueError, 'requester token'):
                    _build_transport({'transport': {'type': transport}})


    def test_explicit_token_generation_never_prints_or_overwrites(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            path = Path(home) / 'new.token'
            command = [sys.executable, '-B', '-m', 'hermes_hostd', 'create-token', str(path)]
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            first = subprocess.run(command, env=env, capture_output=True, timeout=5)
            self.assertEqual(first.returncode, 0, first.stderr.decode())
            self.assertEqual(first.stdout, b'')
            secret = path.read_bytes()
            self.assertGreaterEqual(len(secret), 43)
            self.assertNotIn(secret.strip(), first.stdout + first.stderr)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            second = subprocess.run(command, env=env, capture_output=True, timeout=5)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(path.read_bytes(), secret)
            self.assertNotIn(secret.strip(), second.stdout + second.stderr)

    def test_explicit_insecure_strictness_and_private_sources(self):
        import tempfile
        from hermes_hostd.transport.auth import read_token_file
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home, patch.dict(os.environ, {}, clear=True):
            path = Path(home) / 'requester.token'
            path.write_text('synthetic-file-token\n')
            path.chmod(0o600)
            for kind in ('http', 'vsock'):
                self.assertIsNotNone(_build_transport({'transport': {'type': kind, 'auth_token_file': str(path)}}))
                with self.assertLogs(level='WARNING') as logs:
                    _build_transport({'transport': {'type': kind, 'insecure_no_auth': True}})
                self.assertIn('INSECURE', str(logs.output))
                for invalid in (None, '', False, 0, 'true', [], {}):
                    if invalid is False:
                        continue
                    with self.assertRaises(ValueError):
                        _build_transport({'transport': {'type': kind, 'insecure_no_auth': invalid}})
                with patch.dict(os.environ, {'HOSTD_AUTH_TOKEN': 'synthetic-env-token'}):
                    _build_transport({'transport': {'type': kind}})
                for token in ('', None, True, 1, [], {}, 'x' * 257, 'bad\nvalue', 'é'):
                    with self.assertRaises(ValueError):
                        _build_transport({'transport': {'type': kind, 'auth_token': token}})
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private owned regular file'):
                read_token_file(path)
            path.chmod(0o600)
            link = Path(home) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                read_token_file(link)
            path.write_text('x' * 257)
            with self.assertRaises(ValueError):
                read_token_file(path)


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_vsock_bounded_auth_before_command_allocation(self):
        import asyncio
        import json
        from hermes_hostd.transport.vsock import _VsockProtocol
        token = 'synthetic-requester-credential'
        seen = []
        async def handler(request):
            from hermes_hostd.models import CommandResult
            seen.append(request)
            return CommandResult(request.id, False)
        server = VsockServer(auth_token=token)
        listener = await asyncio.get_running_loop().create_server(
            lambda: _VsockProtocol(handler, auth=server.auth), '127.0.0.1', 0)
        self.addCleanup(listener.close)
        port = listener.sockets[0].getsockname()[1]
        for value in (None, '', 'wrong', True, 1, [], {}, 'x' * 257, token):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            wire = {'id': 'wire', 'description': 'synthetic', 'command': ['echo'], 'auth': {'token': value}}
            writer.write(json.dumps(wire).encode() + b'\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1)
            writer.close()
            await writer.wait_closed()
            self.assertNotIn(token.encode(), response)
            if value != token:
                self.assertIn(b'Unauthorized', response)
                self.assertEqual(seen, [])
        self.assertEqual(len(seen), 1)
        self.assertNotIn('auth', seen[0].to_dict())

    async def test_real_plugin_to_both_daemon_transports_strips_secret_before_approval(self):
        import asyncio
        import importlib.util
        import json
        import socket
        plugin_path = os.environ.get('HOSTD_PLUGIN_CLIENT')
        if not plugin_path:
            self.fail('HOSTD_PLUGIN_CLIENT must select the owned plugin bundled client')
        root = Path(plugin_path).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('wire_auth_plugin', root / '__init__.py', submodule_search_locations=[str(root)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from hermes_hostd.hostd import _make_handler
        from hermes_hostd.models import CommandResult
        from hermes_hostd.transport.vsock import _VsockProtocol
        token = 'synthetic-e2e-requester'
        seen = []
        class Approval:
            async def request_approval(self, operation):
                from hermes_hostd.approval.presentation import render_operation
                seen.append(operation)
                self.assert_secret_absent(operation)
                assert operation.requester_identity is None
                assert token not in render_operation(operation)
                return True
            def assert_secret_absent(self, operation):
                assert token not in repr(operation)
        # Combined integration: plugin-created client -> authenticated daemon ->
        # real approved disposable process, using a private file source.
        import tempfile
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        private = Path(temporary.name)
        token_file = private / 'requester.token'
        token_file.write_text(token + '\n')
        token_file.chmod(0o600)
        executor = CommandExecutor(log_store=LogStore(private / 'logs', 32768))
        handler = _make_handler(Approval(), executor)
        for kind in ('http', 'vsock'):
            if kind == 'http':
                server = HttpServer(port=0, auth_token=token)
                task = asyncio.create_task(server.start(handler))
                for _ in range(100):
                    if server._listener is not None: break
                    await asyncio.sleep(.005)
                port = server._listener.sockets[0].getsockname()[1]
            else:
                server = VsockServer(auth_token=token)
                listener = await asyncio.get_running_loop().create_server(
                    lambda: _VsockProtocol(handler, auth=server.auth), '127.0.0.1', 0)
                port = listener.sockets[0].getsockname()[1]
            real_socket = socket.socket
            class FixtureSocket:
                def __init__(self): self.sock = real_socket()
                def settimeout(self, value): self.sock.settimeout(value)
                def connect(self, address): self.sock.connect(('127.0.0.1', port))
                def sendall(self, value): self.sock.sendall(value)
                def recv(self, size): return self.sock.recv(size)
                def close(self): self.sock.close()
            try:
                with patch.dict(os.environ, {'HOSTD_AUTH_TOKEN_FILE': str(token_file), 'HOSTD_TRANSPORT': kind,
                         'HOSTD_HTTP_URL': f'http://127.0.0.1:{port}/execute', 'HERMES_HOME': str(private)}, clear=True), patch.object(
                            module._client_mod.socket, 'socket', side_effect=lambda *a, **k:
                            FixtureSocket() if a and a[0] == socket.AF_VSOCK else real_socket(*a, **k)):
                    before = len(seen)
                    with patch.dict(os.environ, {'HOSTD_AUTH_TOKEN': 'synthetic-wrong'}):
                        rejected = await asyncio.to_thread(module._handle_run_on_host,
                            {'command': ['echo'], 'description': 'synthetic'})
                    self.assertFalse(json.loads(rejected).get('approved', False))
                    self.assertEqual(len(seen), before)
                    with patch.dict(os.environ, {'HOSTD_AUTH_TOKEN': ''}):
                        rejected = await asyncio.to_thread(module._handle_run_on_host,
                            {'command': ['echo'], 'description': 'synthetic'})
                    self.assertFalse(json.loads(rejected).get('success', False))
                    self.assertEqual(len(seen), before)
                    with self.assertLogs('hermes-hostd', level='INFO') as captured:
                        result = await asyncio.to_thread(module._handle_run_on_host,
                            {'command': ['/usr/bin/printf', 'inert-success'], 'description': 'synthetic'})
                    self.assertNotIn(token, repr(captured.output))
                self.assertNotIn(token, result)
                self.assertEqual(json.loads(result)['stdout'], 'inert-success')
            finally:
                if kind == 'http':
                    await server.stop()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    listener.close()
                    await listener.wait_closed()
        self.assertEqual(len(seen), 2)

    async def test_vsock_missing_token_rejected_and_insecure_legacy_accepted(self):
        import asyncio
        from hermes_hostd.transport.vsock import _VsockProtocol
        from hermes_hostd.transport.auth import RequesterAuth
        from hermes_hostd.models import CommandResult
        from test_admission import Transport
        seen = []
        async def handler(request):
            seen.append(request)
            return CommandResult(request.id, False)
        for insecure in (False, True):
            auth = RequesterAuth(None if insecure else 'synthetic', insecure_no_auth=insecure)
            protocol = _VsockProtocol(handler, auth=auth)
            wire = Transport()
            protocol.connection_made(wire)
            protocol.data_received(b'{"id":"legacy","description":"inert","command":["echo"]}\n')
            await asyncio.sleep(.01)
            self.assertTrue(wire.closed)
            self.assertEqual(len(seen), int(insecure))
            if not insecure:
                self.assertIn(b'Unauthorized', wire.output)

    async def test_vsock_duplicate_auth_and_identity_metadata_rejected(self):
        import asyncio
        import json
        from hermes_hostd.transport.vsock import _VsockProtocol
        from hermes_hostd.models import CommandRequest
        from test_admission import Transport
        from hermes_hostd.transport.auth import RequesterAuth
        for metadata in ('null', '[]', 'true', '"synthetic"', '{}',
                         '{"token":"synthetic","identity":"admin"}',
                         '{"token":"wrong","token":"synthetic"}'):
            wire = Transport()
            protocol = _VsockProtocol(None, auth=RequesterAuth('synthetic'))
            protocol.connection_made(wire)
            with patch.object(CommandRequest, 'from_dict', side_effect=AssertionError('allocation before auth')):
                protocol.data_received(('{"auth":' + metadata + ',"description":"inert","command":["echo"]}\n').encode())
                await asyncio.sleep(.01)
            self.assertNotIn(b'synthetic', wire.output)
            self.assertTrue(wire.closed)
            self.assertNotIn(b'internal error', wire.output)

    async def test_http_rejects_before_aiohttp_allocation_or_body_read(self):
        import asyncio
        from hermes_hostd.transport.http_gate import HttpGate
        server = HttpServer(auth_token='synthetic-requester-credential')
        allocated = []
        def factory():
            allocated.append(True)
            raise AssertionError('unauthorized parser allocation')
        listener = await asyncio.get_running_loop().create_server(
            lambda: HttpGate(server, factory), '127.0.0.1', 0)
        self.addCleanup(listener.close)
        for header in (b'', b'Authorization: Bearer wrong\r\n'):
            reader, writer = await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1])
            writer.write(b'POST /execute HTTP/1.1\r\nHost: local\r\nContent-Length: 42\r\n' + header + b'\r\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), .2)
            self.assertIn(b'401', response)
            self.assertEqual(allocated, [])
            writer.close()
            await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
