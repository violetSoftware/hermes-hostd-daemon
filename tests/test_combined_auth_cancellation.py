"""Combined authentication and cancellation boundaries using authenticated disposable streams."""
import asyncio
import json
import test_cancellation
from test_cancellation import until
from hermes_hostd.approval.base import ApprovalChannel
from hermes_hostd.transport.vsock import VsockServer


class AuthenticatedCancellationTests(test_cancellation.WireTests):
    """Run every wire lifecycle check with required authentication enabled."""

    TOKEN = 'synthetic-integration-requester'


    async def test_vsock_factory_carries_auth_and_lifecycle_ownership(self):
        server = VsockServer(auth_token=self.TOKEN)
        protocol = server._protocol(self.handler)
        self.assertIs(protocol._auth, server.auth)
        self.assertIs(protocol._tasks, server._tasks)
        self.assertIs(protocol._connections, server._connections)

    async def test_missing_wrong_auth_never_approves_or_spawns_and_slot_recovers(self):
        for kind in ('http', 'vsock'):
            approvals = []
            class Approval(ApprovalChannel):
                async def request_approval(self, request):
                    approvals.append(request)
                    return True
            from hermes_hostd.hostd import _make_handler
            handler = _make_handler(Approval(), self.executor)
            for token in (None, 'synthetic-wrong'):
                server, reader, writer = await self.open_wire(kind, handler)
                wire = self.frame(kind, self.request())
                if kind == 'http':
                    wire = wire.replace(b'Authorization: Bearer ' + self.TOKEN.encode() + b'\r\n',
                        b'' if token is None else b'Authorization: Bearer ' + token.encode() + b'\r\n')
                else:
                    body = json.loads(wire)
                    if token is None:
                        body.pop('auth')
                    else:
                        body['auth']['token'] = token
                    wire = json.dumps(body).encode() + b'\n'
                writer.write(wire)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 1)
                self.assertIn(b'Unauthorized', response)
                self.assertNotIn(self.TOKEN.encode(), response)
                self.assertEqual(approvals, [])
                self.assertFalse((self.root / 'parent').exists())
                await until(lambda: server.admission.used == 0)
                await server.stop()
            # Authentication does not consume a challenge/ID or pending slot.
            from hermes_hostd.models import CommandRequest
            server, reader, writer = await self.open_wire(kind, handler)
            writer.write(self.frame(kind, CommandRequest('tree', 'approved', ['/usr/bin/true'], 0)))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            if kind == 'http':
                response = response.split(b'\r\n\r\n', 1)[1]
            self.assertEqual(json.loads(response)['exit_code'], 0)
            self.assertEqual(len(approvals), 1)
            self.assertIsNone(approvals[0].requester_identity)
            self.assertNotIn(self.TOKEN, repr(approvals[0]))
            await server.stop()

    async def test_disconnect_at_auth_and_body_boundaries_never_approves(self):
        for kind in ('http', 'vsock'):
            for stage in ('auth', 'body'):
                approvals = []
                class Approval(ApprovalChannel):
                    async def request_approval(self, request):
                        approvals.append(request)
                        return True
                from hermes_hostd.hostd import _make_handler
                server, reader, writer = await self.open_wire(kind, _make_handler(Approval(), self.executor))
                wire = self.frame(kind, self.request())
                if kind == 'http':
                    cut = wire.index(b'Bearer ') + 8 if stage == 'auth' else wire.index(b'\r\n\r\n') + 8
                else:
                    cut = wire.index(b'"token"') + 8 if stage == 'auth' else len(wire) - 2
                writer.write(wire[:cut])
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                await until(lambda: server.admission.used == 0)
                await asyncio.sleep(.03)
                self.assertEqual(approvals, [])
                self.assertFalse((self.root / 'parent').exists())
                await server.stop()

    async def test_authenticated_denial_never_spawns(self):
        for kind in ('http', 'vsock'):
            class Deny(ApprovalChannel):
                async def request_approval(self, request):
                    return False
            from hermes_hostd.hostd import _make_handler
            server, reader, writer = await self.open_wire(kind, _make_handler(Deny(), self.executor))
            writer.write(self.frame(kind, self.request()))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1)
            if kind == 'http':
                response = response.split(b'\r\n\r\n', 1)[1]
            self.assertFalse(json.loads(response)['approved'])
            self.assertFalse((self.root / 'parent').exists())
            await server.stop()
