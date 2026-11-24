"""Offline regression only; never import the executable business entrypoint."""
from pathlib import Path
import sys
import unittest
import contextlib
import io
import json

root = Path(__file__).resolve().parents[1]
for relative in ('workspace/skills/jingye-reconciliation/scripts',
                 'workspace/skills/jingye-platform-sync/scripts', 'operations', 'tests',
                 'workspace/skills/jingye-reconciliation/tests',
                 'workspace/skills/jingye-platform-sync/tests'):
    sys.path.insert(0, str(root / relative))
suite = unittest.TestSuite()
for relative in ('tests', 'workspace/skills/jingye-reconciliation/tests',
                 'workspace/skills/jingye-platform-sync/tests'):
    suite.addTests(unittest.TestLoader().discover(str(root / relative)))
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
(root.parent/'offline-tests.txt').write_text(stream.getvalue(), encoding='utf-8')
summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
               skipped=len(result.skipped), passed=result.wasSuccessful())
(root.parent/'offline-tests.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
print(json.dumps(summary))
for test, detail in result.errors+result.failures:
    print(str(test)+': '+detail.splitlines()[-1])
raise SystemExit(0 if result.wasSuccessful() else 1)
