"""Archive-member validation for verified cross-toolchain handoffs."""
import importlib.util
import io
from pathlib import Path
import tarfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/bootstrap/temporary_tools/temp_tools_bootstrap.py'
spec = importlib.util.spec_from_file_location('temp_tools_bootstrap_archive_test', SOURCE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def archive_with_link(target, *, symlink=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        data = b'compiler payload'
        entry = tarfile.TarInfo('usr/bin/compiler')
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
        other = tarfile.TarInfo('usr/bin/compiler++')
        other.type = tarfile.SYMTYPE if symlink else tarfile.LNKTYPE
        other.linkname = target
        archive.addfile(other)
    buffer.seek(0)
    return buffer


class ArchiveMemberTests(unittest.TestCase):
    def test_hard_link_to_prior_regular_file_is_accepted(self):
        with tarfile.open(fileobj=archive_with_link('usr/bin/compiler'), mode='r') as archive:
            members = bootstrap.archive_members(archive)
        self.assertEqual(len(members), 2)
        self.assertTrue(members[1].islnk())

    def test_hard_link_to_missing_file_is_rejected(self):
        with tarfile.open(fileobj=archive_with_link('usr/bin/missing'), mode='r') as archive:
            with self.assertRaisesRegex(RuntimeError, 'unsafe archive hard link'):
                bootstrap.archive_members(archive)

    def test_hard_link_path_traversal_is_rejected(self):
        with tarfile.open(fileobj=archive_with_link('../../etc/passwd'), mode='r') as archive:
            with self.assertRaisesRegex(RuntimeError, 'unsafe archive hard link'):
                bootstrap.archive_members(archive)

    def test_symlink_cannot_be_a_hard_link_target(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            first = tarfile.TarInfo('usr/bin/link')
            first.type = tarfile.SYMTYPE
            first.linkname = 'compiler'
            archive.addfile(first)
            second = tarfile.TarInfo('usr/bin/alias')
            second.type = tarfile.LNKTYPE
            second.linkname = 'usr/bin/link'
            archive.addfile(second)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode='r') as archive:
            with self.assertRaisesRegex(RuntimeError, 'unsafe archive hard link'):
                bootstrap.archive_members(archive)



class HandoffHardlinkTests(unittest.TestCase):
    """Imported hardlink collisions must be verified, not bypassed."""

    def test_archive_hardlink_collision_validation(self):
        import hashlib
        import json
        import os
        import sys
        import tempfile
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from bootstrap.temporary_tools import package_runner

        def scenario(*, hardlink_target='usr/bin/compiler', mutate=False,
                     independent=False, extra_target=True):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, stage = base / 'root', base / 'stage'
                (root / 'usr/bin').mkdir(parents=True)
                (stage / 'usr/bin').mkdir(parents=True)
                (root / 'usr/libexec/gcc/x86_64-distro-linux-gnu/16.2.0').mkdir(parents=True)
                (stage / 'usr/libexec/gcc/x86_64-distro-linux-gnu/16.2.0').mkdir(parents=True)
                (root / 'usr/bin/compiler').write_bytes(b'original')
                alias = root / 'usr/bin/compiler++'
                if independent:
                    alias.write_bytes(b'original')
                else:
                    os.link(root / 'usr/bin/compiler', alias)
                if mutate:
                    alias.write_bytes(b'changed')
                (stage / 'usr/bin/compiler++').write_bytes(b'staged')
                # Required compiler-private transfer evidence.
                (root / 'usr/libexec/gcc/x86_64-distro-linux-gnu/16.2.0/cc1').write_bytes(b'old cc1')
                (stage / 'usr/libexec/gcc/x86_64-distro-linux-gnu/16.2.0/cc1').write_bytes(b'new cc1')
                marker_dir = base / '.distro-bootstrap'
                marker_dir.mkdir()
                archive_path = base / 'cross-toolchain-root.tar.xz'
                with tarfile.open(archive_path, 'w:xz') as archive:
                    entry = tarfile.TarInfo('usr/bin/compiler')
                    entry.size = len(b'original')
                    archive.addfile(entry, io.BytesIO(b'original'))
                    entry = tarfile.TarInfo('usr/bin/compiler++')
                    entry.type = tarfile.LNKTYPE
                    entry.linkname = hardlink_target
                    archive.addfile(entry)
                    entry = tarfile.TarInfo('usr/libexec/gcc/x86_64-distro-linux-gnu/16.2.0/cc1')
                    entry.size = len(b'old cc1')
                    archive.addfile(entry, io.BytesIO(b'old cc1'))
                (marker_dir / 'temp-tools-import.json').write_text(json.dumps({
                    'cross_archive_sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest()}))
                manifest = {'target': 'x86_64-distro-linux-gnu'}
                pkg = {'version': '16.2.0'}
                policy = {'compiler_private_prefix': 'manifest-target-and-version'}
                with patch.object(package_runner.common, 'install', return_value={}):
                    return package_runner.install_handoff(stage, root, policy, pkg, manifest, archive_path)

        scenario()
        for kwargs in ({'hardlink_target': '../escape'},
                       {'hardlink_target': 'usr/bin/missing'},
                       {'hardlink_target': 'usr/bin/compiler++'},
                       {'mutate': True}, {'independent': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                scenario(**kwargs)


if __name__ == '__main__':
    unittest.main()
