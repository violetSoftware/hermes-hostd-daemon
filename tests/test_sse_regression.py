"""Synthetic SSE regression: invalid envelopes do not consume authority; bounds close."""
import asyncio
import json
from pathlib import Path
import sys
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.approval.ntfy import _Challenge, _sse_listener, SSE_EVENT_BYTES, SSE_LINE_BYTES

class SSERegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_envelopes_continue_to_valid_and_close(self):
        loop = asyncio.get_running_loop()
        challenge = _Challenge('a'*64, 'b'*64, 1900000060, time.monotonic()+10, loop, loop.create_future())
        bad = ['null','[]','1','"text"','{',json.dumps({'message':[]}),json.dumps({'message':'[]'}),json.dumps({'event':'keepalive','message':json.dumps(challenge.body('approved'))})]
        valid = json.dumps({'event':'message','message':json.dumps(challenge.body('denied'))})
        response = Response([('data: '+p+'\n\n').encode() for p in bad+[valid]])
        await _sse_listener('http://broker.invalid','fixture',challenge,None,Session(response))
        await asyncio.sleep(0)
        self.assertEqual(challenge.future.result(),'denied')
        self.assertTrue(response.closed)

    async def test_line_and_multiline_event_limits_close_without_decision(self):
        for chunks in ([b'x'*(SSE_LINE_BYTES+1)], [b'data: '+b'x'*6000+b'\n']*3):
            with self.subTest(multiline=len(chunks)>1):
                self.assertGreater(sum(len(c) for c in chunks), min(SSE_LINE_BYTES,SSE_EVENT_BYTES))
                loop=asyncio.get_running_loop()
                challenge=_Challenge('a'*64,'b'*64,1900000060,time.monotonic()+10,loop,loop.create_future())
                response=Response(chunks)
                with self.assertRaisesRegex(ValueError,'exceeds limit'):
                    await _sse_listener('http://broker.invalid','fixture',challenge,None,Session(response))
                self.assertTrue(response.closed)
                self.assertFalse(challenge.future.done())
                challenge.revoke()
                self.assertFalse(challenge.consume(challenge.body('approved')))

class Response:
    status=200
    def __init__(self,chunks): self.chunks=chunks;self.content=self;self.closed=False
    async def __aenter__(self): return self
    async def __aexit__(self,*args): self.closed=True
    async def iter_chunked(self,size):
        for chunk in self.chunks:
            for i in range(0,len(chunk),size): yield chunk[i:i+size]
class Session:
    def __init__(self,response): self.response=response
    def get(self,*args,**kwargs): return self.response

if __name__=='__main__': unittest.main()
