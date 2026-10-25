"""FS-001 native archive validation tests."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bootstrap import temp_tools_validation as validation


class NativeArchiveTests(unittest.TestCase):
    def test_manifest_script_is_native_and_staged(self):
        recipe = {'steps': [
            {'name': 'configure', 'argv': ['/work/src/configure', '--host={target}',
                                           'CC={cc}', '--prefix=/usr']},
            {'name': 'build', 'argv': ['make', '-j{jobs}']},
            {'name': 'install', 'argv': ['make', 'install', 'DESTDIR=/work/stage']}]}
        script = validation.build_script('bash', '5.3', recipe, Path('/sources/bash-5.3'), [])
        self.assertIn('/sources/bash-5.3/configure', script)
        self.assertIn('DESTDIR=/build/stage', script)
        self.assertNotIn('/toolchain', script)
        self.assertNotIn('--host=', script)

    def test_source_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / 'source.tar.xz'
            with tarfile.open(a, 'w:xz') as tf:
                item = tarfile.TarInfo('pkg-1/../../escape')
                item.size = 1
                tf.addfile(item, io.BytesIO(b'x'))
            with tarfile.open(a, 'r:xz') as tf:
                with self.assertRaisesRegex(RuntimeError, 'unsafe package source'):
                    validation.safe_source_archive(tf, 'pkg-1')

    def test_verified_extracted_hardlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'usr/bin').mkdir(parents=True)
            a = root / 'usr/bin/a'
            b = root / 'usr/bin/b'
            a.write_bytes(b'abc')
            os.link(a, b)
            inventory = {'usr': ('dir', None), 'usr/bin': ('dir', None),
                         'usr/bin/a': ('file', hashlib.sha256(b'abc').hexdigest()),
                         'usr/bin/b': ('hardlink', 'usr/bin/a')}
            validation.verify_extracted(root, inventory)
            b.unlink()
            b.write_bytes(b'abc')
            with self.assertRaisesRegex(RuntimeError, 'hardlink differs'):
                validation.verify_extracted(root, inventory)


if __name__ == '__main__':
    unittest.main()
