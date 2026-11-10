"""Run with the clean installed Python from a non-checkout cwd.

Usage: /installed/venv/bin/python /checkout/tests/verify_installed.py WHEEL
"""
import importlib
import importlib.metadata
from pathlib import Path
import subprocess
import sys
import zipfile

wheel = Path(sys.argv[1])
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    forbidden = ('client/', 'approval/', 'transport/', 'models.py', 'hostd.py', 'executor.py')
    assert not any(name.startswith(forbidden) for name in names), names
    assert any(name.endswith('/share/hermes-hostd/systemd/hermes-hostd.service') for name in names), names
    assert not any(name.endswith(('.yaml', '.yml')) for name in names), names
    print('Wheel members:', len(names), '; namespaced modules/metadata/new unit')
package = importlib.import_module('hermes_hostd')
assert package.__file__ and 'site-packages' in package.__file__, package.__file__
for name in ('models', 'approval', 'transport', 'executor', 'hostd'):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError:
        continue
    raise AssertionError('Generic top-level import leaked: ' + name)
print('Installed import:', package.__file__)
print('Installed version:', importlib.metadata.version('hermes-hostd'))
for command in ([str(Path(sys.executable).with_name('hostd')), '--help'],
                [sys.executable, '-m', 'hermes_hostd', '--help']):
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert '--config' in result.stdout, result.stdout
    print('PASS entrypoint:', ' '.join(command))
