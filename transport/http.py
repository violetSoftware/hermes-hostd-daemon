"""Compatibility import for hermes_hostd.transport.http."""
import importlib
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
sys.modules[__name__] = importlib.import_module('hermes_hostd.transport.http')
