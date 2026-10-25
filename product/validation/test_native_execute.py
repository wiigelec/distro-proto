import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

module = importlib.import_module('product.src.bootstrap.temporary_tools.native_execute')


class ExecuteTests(unittest.TestCase):
    def test_existing_output_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for item in ('root', 'inputs', 'inputs/scripts', 'inputs/sources', 'output'):
                (base / item).mkdir(parents=True, exist_ok=True)
            with mock.patch.object(module.os, 'geteuid', return_value=1000), \
                 mock.patch.object(module.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'fresh'):
                    module.execute(base / 'root', base / 'archive', base / 'evidence',
                                   base / 'inputs', base / 'output')
                run.assert_not_called()

    def test_root_execution_refused(self):
        with mock.patch.object(module.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'ordinary user'):
                module.execute('/missing', '/missing', '/missing', '/missing', '/missing')


if __name__ == '__main__':
    unittest.main()
