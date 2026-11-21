"""Run isolated Python tests without generating bytecode in the source tree."""
from pathlib import Path
import sys
import unittest
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[1]
suite=unittest.defaultTestLoader.discover(str(root/'test'),pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
