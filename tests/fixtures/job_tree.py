"""Bounded real descendants, self-expiring even if the implementation fails."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(sys.argv[1])
mode = sys.argv[2]
role = sys.argv[3] if len(sys.argv) > 3 else 'parent'
(root / role).write_text(str(os.getpid()))
if mode == 'ignore':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = None
if role != 'grandchild':
    child = subprocess.Popen([sys.executable, __file__, str(root), mode,
                              'child' if role == 'parent' else 'grandchild'])
if mode == 'closed':
    os.close(1)
    os.close(2)
if mode == 'leader-exit' and role == 'parent':
    raise SystemExit(0)
time.sleep(3)
(root / (role + '-orphan')).write_text('survived')
if child is not None:
    child.wait(timeout=4)
