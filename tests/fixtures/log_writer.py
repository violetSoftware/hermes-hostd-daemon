"""Synthetic subprocess writer for interprocess locking and restart tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from hermes_hostd.disk_logs import LogStore

store = LogStore(sys.argv[1], int(sys.argv[2]))
job = store.new_id()
for offset in range(100):
    store.append(job, 'stderr', offset * 2000, b'x' * 2000)
print(job)
