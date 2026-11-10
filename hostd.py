#!/usr/bin/env python3
"""Compatibility launcher; canonical release source is src/hermes_hostd."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from hermes_hostd import hostd as _implementation
_implementation._DEFAULT_CONFIG_PATHS[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
if __name__ == '__main__':
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
