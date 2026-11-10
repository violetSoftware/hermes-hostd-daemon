"""Synthetic private stores only; no installed daemon/config/log access."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class StoreTests(unittest.TestCase):
    def test_budget_rotation_restart_and_attribution(self):
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', budget=16384)
            job = store.new_id()
            store.append(job, 'stdout', 0, b'A' * 3000)
            store.append(job, 'stderr', 0, b'err')
            store.append(job, 'stdout', 3000, b'B' * 3000)
            store.append(job, 'stdout', 6000, b'C' * 3000)
            self.assertLessEqual(sum(p.stat().st_size for p in (Path(tmp) / 'logs').iterdir()), 16384)
            recovered = LogStore(Path(tmp) / 'logs', budget=16384)
            data = recovered.read(job, 'stdout', 0, 65536)
            self.assertEqual(data['data'], b'B' * 3000 + b'C' * 3000)
            self.assertTrue(data['gap'])
            self.assertEqual(data['next_offset'], 9000)
            self.assertEqual(recovered.read(job, 'stderr', 0, 20)['data'], b'err')


    def test_cleanup_removes_old_payload_bytes_on_slot_reuse(self):
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 12288)
            old, other = store.new_id(), store.new_id()
            store.append(old, 'stdout', 0, b'SECRET' * 500)
            store.append(other, 'stdout', 0, b'x')
            store.append(other, 'stdout', 1, b'y')
            store.cleanup(other)
            self.assertNotIn(b'SECRET', (store.directory / 'ring-v1').read_bytes())
            store.append(old, 'stdout', 0, b'SECRET' * 500)
            store.append(other, 'stdout', 2, b'z')
            store.append(other, 'stdout', 3, b'z')
            self.assertNotIn(b'SECRET', (store.directory / 'ring-v1').read_bytes())

    def test_concurrent_processes_share_budget_and_restart_retention(self):
        import subprocess
        from concurrent.futures import ThreadPoolExecutor
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 65536)
            old = store.new_id()
            store.append(old, 'stdout', 0, b'completed')
            def write(index):
                local = LogStore(store.directory, store.budget)
                job = local.new_id()
                for offset in range(100):
                    local.append(job, 'stderr', offset * 2000, bytes([index]) * 2000)
                return job
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = list(pool.map(write, range(1, 5)))
            self.assertEqual(store.read(old, 'stdout')['data'], b'')
            self.assertLessEqual((store.directory / 'ring-v1').stat().st_blocks * 512, store.budget)
            self.assertLessEqual(sum(p.stat().st_size for p in store.directory.iterdir()), store.budget)
            self.assertTrue(any(LogStore(store.directory, store.budget).read(job, 'stderr')['data'] for job in jobs))
            commands = [[sys.executable, '-B', str(Path(__file__).parent / 'fixtures/log_writer.py'),
                         str(store.directory), str(store.budget)] for _ in range(3)]
            processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
            for process in processes:
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                jobs.append(output.decode().strip())
            self.assertLessEqual((store.directory / 'ring-v1').stat().st_blocks * 512, store.budget)
            self.assertTrue(any(LogStore(store.directory, store.budget).read(job, 'stderr')['data'] for job in jobs))
            store.cleanup()
            for job in jobs:
                self.assertEqual(store.read(job, 'stderr')['data'], b'')

    def test_torn_header_is_not_returned_as_another_stream_or_offset(self):
        from hermes_hostd.disk_logs import LogStore, PAGE
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 16384)
            job = store.new_id()
            store.append(job, 'stdout', 0, b'payload')
            fd = os.open(store.directory / 'ring-v1', os.O_RDWR)
            try:
                os.pwrite(fd, b'e', PAGE + 24)  # corrupt only attribution, not payload
            finally:
                os.close(fd)
            recovered = LogStore(store.directory, 16384)
            self.assertEqual(recovered.read(job, 'stderr')['data'], b'')

    def test_partial_writes_disk_failure_and_recovery(self):
        import errno
        from unittest.mock import patch
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 16384)
            job = store.new_id()
            real = os.pwrite
            def partial(fd, data, offset):
                return real(fd, data[:7], offset)
            with patch('hermes_hostd.disk_logs.os.pwrite', side_effect=partial):
                store.append(job, 'stdout', 0, b'partial')
            self.assertEqual(store.read(job, 'stdout')['data'], b'partial')
            def full(fd, data, offset):
                if offset >= 4096:
                    raise OSError(errno.ENOSPC, 'synthetic disk full')
                return real(fd, data, offset)
            with patch('hermes_hostd.disk_logs.os.pwrite', side_effect=full):
                with self.assertRaises(OSError):
                    store.append(job, 'stdout', 7, b'not written')
            recovered = LogStore(store.directory, 16384)
            recovered.append(job, 'stdout', 18, b'recovery')
            read = recovered.read(job, 'stdout')
            self.assertEqual(read['data'], b'partialrecovery')
            self.assertTrue(read['gap'])
            self.assertEqual(read['next_offset'], 26)

    def test_chunks_offsets_validation_and_private_files(self):
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 32768)
            job = store.new_id()
            store.append(job, 'stdout', 0, b'abcdef')
            self.assertEqual(store.read(job, 'stdout', 2, 2)['data'], b'cd')
            self.assertEqual(store.read(job, 'stdout', 2, 2)['next_offset'], 4)
            for bad in ('../secret', '', 'A' * 32):
                with self.assertRaises(ValueError):
                    store.read(bad, 'stdout')
            for limit in (0, 65537, -1, True):
                with self.assertRaises(ValueError):
                    store.read(job, 'stdout', limit=limit)
            self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((store.directory / 'ring-v1').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(OSError):
                LogStore(store.directory, 16384)
            (Path(tmp) / 'symlink').symlink_to(store.directory, target_is_directory=True)
            with self.assertRaises(OSError):
                LogStore(Path(tmp) / 'symlink', 32768)
            (store.directory / 'ring-v1').chmod(0o644)
            with self.assertRaises(PermissionError):
                LogStore(store.directory, 32768)


class ExecutorTests(unittest.TestCase):
    def test_simultaneous_executions_share_completed_log_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 32768)
            executor = CommandExecutor(log_store=store)
            completed = executor.execute(CommandRequest('old', 'synthetic', ['/usr/bin/printf', 'completed'], 0))
            def run(index):
                return executor.execute(CommandRequest(str(index), 'synthetic', [sys.executable, '-c',
                    "import os; [(os.write(1,b'a'*8192),os.write(2,b'b'*8192)) for _ in range(64)]"], 10))
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(run, range(3)))
            self.assertTrue(all(result.exit_code == 0 and result.error is None for result in results))
            self.assertEqual(len({result.log_id for result in results}), 3)
            self.assertEqual(store.read(completed.log_id, 'stdout')['data'], b'')
            self.assertLessEqual((store.directory / 'ring-v1').stat().st_blocks * 512, store.budget)

    def test_write_failure_remains_visible_if_command_also_times_out(self):
        from unittest.mock import patch
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 32768)
            with patch.object(store, 'append', side_effect=OSError('synthetic ENOSPC')):
                result = CommandExecutor(log_store=store).execute(CommandRequest('both', 'synthetic',
                    [sys.executable, '-c', "import os,time; os.write(1,b'x'); time.sleep(2)"], 1))
            self.assertIn('logging failed', result.error)
            self.assertIn('timed out', result.error)
            self.assertIsNone(result.exit_code)

    def test_write_failure_is_explicit_without_fallback_and_child_is_drained(self):
        import errno
        from unittest.mock import patch
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 32768)
            with patch.object(store, 'append', side_effect=OSError(errno.ENOSPC, 'synthetic disk full')) as writing:
                result = CommandExecutor(log_store=store).execute(CommandRequest('failure', 'synthetic',
                    [sys.executable, '-c', "import os; [os.write(1,b'x'*8192) for _ in range(128)]"], 10))
            self.assertEqual(writing.call_count, 1)
            self.assertEqual(result.exit_code, 0)
            self.assertIn('output logging failed', result.error)
            self.assertTrue(result.output_truncated)
            self.assertLess(len(result.stdout), 200)

    def test_binary_stream_fidelity_failure_exit_and_unlimited(self):
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 32768)
            result = CommandExecutor(log_store=store).execute(CommandRequest('binary', 'synthetic',
                [sys.executable, '-c', "import os; os.write(1,b'out\\x00\\xff'); os.write(2,b'err\\r\\n'); raise SystemExit(4)"], 0))
            self.assertEqual(result.exit_code, 4)
            self.assertIsNone(result.error)
            self.assertFalse(result.output_truncated)
            self.assertEqual(result.stdout, 'out\x00\ufffd')
            self.assertEqual(result.stderr, 'err\n')
            self.assertEqual(store.read(result.log_id, 'stdout')['data'], b'out\x00\xff')
            self.assertEqual(store.read(result.log_id, 'stderr')['data'], b'err\r\n')

    def test_sustained_output_python_memory_is_bounded(self):
        import tracemalloc
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 131072)
            tracemalloc.start()
            try:
                result = CommandExecutor(log_store=store).execute(CommandRequest('memory', 'synthetic',
                    [sys.executable, '-c', "import os; [os.write(1,b'x'*8192) for _ in range(4096)]"], 30))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(result.exit_code, 0)
            self.assertLess(peak, 2 * 1024 * 1024)
            self.assertLess(len(result.stdout), 16600)
            print(f'32 MiB synthetic output, tracemalloc peak={peak} bytes, response={len(result.stdout)} chars')

    def test_large_output_has_bounded_tails_and_rotation_notice(self):
        from hermes_hostd.disk_logs import LogStore
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest, CommandResult
        with tempfile.TemporaryDirectory() as tmp:
            executor = CommandExecutor(log_store=LogStore(Path(tmp) / 'logs', 32768), tail_bytes=1024)
            result = executor.execute(CommandRequest('large', 'synthetic', [sys.executable, '-c',
                "import os; [(os.write(1,b'A'*8192),os.write(2,b'B'*8192)) for _ in range(128)]"], 10))
            self.assertEqual(result.exit_code, 0)
            self.assertLessEqual(len(result.stdout), 1200)
            self.assertLessEqual(len(result.stderr), 1200)
            self.assertIn('truncated', result.stdout)
            self.assertTrue(result.output_truncated)
            self.assertEqual(len(result.log_id), 32)
            self.assertEqual(CommandResult.from_dict(result.to_dict()).log_id, result.log_id)


class CliTests(unittest.TestCase):
    def test_scoped_bounded_retrieval_and_cleanup(self):
        import base64
        import json
        import subprocess
        from hermes_hostd.disk_logs import LogStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LogStore(Path(tmp) / 'logs', 16384)
            job = store.new_id()
            store.append(job, 'stdout', 0, b'hello\x00\xff')
            config = Path(tmp) / 'fixture.json'
            config.write_text(json.dumps({'executor': {'logs': {'directory': str(store.directory), 'budget_bytes': 16384}}}))
            command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'hostd.py'),
                       '--config', str(config), 'logs']
            result = subprocess.run(command + ['read', job, '--limit', '3'], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(base64.b64decode(payload['data_base64']), b'hel')
            self.assertEqual(payload['next_offset'], 3)
            bad = subprocess.run(command + ['read', '../arbitrary', '--limit', '65537'], capture_output=True, timeout=5)
            self.assertNotEqual(bad.returncode, 0)
            clean = subprocess.run(command + ['cleanup', job], capture_output=True, timeout=5)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertEqual(store.read(job, 'stdout')['data'], b'')


if __name__ == '__main__':
    unittest.main()
