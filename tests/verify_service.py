"""Verify new template only, with executable resolved in a scratch copy.

Usage: python tests/verify_service.py /absolute/installed/venv/bin/hostd
Does not install a service or inspect any deployed unit.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
executable = Path(sys.argv[1]).resolve()
assert executable.is_file() and os.access(executable, os.X_OK), executable
text = (root / 'packaging/systemd/hermes-hostd.service').read_text()
with tempfile.TemporaryDirectory(prefix='hostd-unit-', dir=os.environ['TMPDIR']) as tmp:
    unit = Path(tmp) / 'hermes-hostd.service'
    # Replace only missing install-location executable, not other directives.
    unit.write_text(text.replace('%h/.local/share/hermes-hostd/venv/bin/hostd', str(executable)))
    result = subprocess.run(['systemd-analyze', '--user', 'verify', str(unit)], text=True,
                            capture_output=True)
    print(result.stdout + result.stderr)
    print('Scratch-resolved template verification exit:', result.returncode)
    raise SystemExit(result.returncode)
