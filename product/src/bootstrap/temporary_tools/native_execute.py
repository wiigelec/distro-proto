"""Run native package construction inside an isolated, verified tool root.

The host controls only input verification and namespace setup. The native
assembler executes the complete ordered build from within the tool root.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .package_runner import verify_temporary_tools_archive


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def execute(root, archive, evidence, inputs, output):
    root, archive, evidence, inputs, output = [Path(p).expanduser().absolute() for p in
                                               (root, archive, evidence, inputs, output)]
    if os.geteuid() == 0:
        raise RuntimeError('native construction must run as an ordinary user')
    for directory in (root, inputs, inputs / 'scripts', inputs / 'sources'):
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError('missing or linked input directory: ' + str(directory))
    if output.exists() or output.is_symlink():
        raise RuntimeError('construction workspace must be fresh')
    if not archive.is_file() or archive.is_symlink() or evidence.is_symlink():
        raise RuntimeError('invalid tool archive or evidence')
    record = json.loads(evidence.read_text())
    if record.get('status') != 'verified' or record.get('handoff', {}).get('sha256') != digest(archive):
        raise RuntimeError('tool archive checksum differs from verified evidence')
    count = verify_temporary_tools_archive(root, archive)
    if record['handoff'].get('members') != count:
        raise RuntimeError('tool archive member count differs from evidence')
    required = ('usr/bin/bash', 'usr/bin/env', 'usr/bin/make', 'usr/bin/gcc',
                'usr/bin/cp', 'usr/bin/readlink')
    if any(not (root / name).is_file() for name in required):
        raise RuntimeError('native tool root is missing required executables')
    plan = inputs / 'scripts' / 'plan.tsv'
    if not plan.is_file() or plan.is_symlink():
        raise RuntimeError('missing native build plan')
    for line in plan.read_text().splitlines():
        fields = line.split('\t')
        if len(fields) != 2 or fields[1] != f'/inputs/scripts/{fields[0]}.sh':
            raise RuntimeError('native plan has an unexpected script path')
        name = fields[0]
        if not name or '/' in name or name in ('.', '..') or not name.replace('-', '').replace('_','').replace('.','').isalnum():
            raise RuntimeError('invalid package name in plan')
        if not (inputs / 'scripts' / (name + '.sh')).is_file() or not (inputs / 'sources' / name / 'configure').is_file():
            raise RuntimeError('native plan inputs incomplete: ' + name)
    script = Path(__file__).with_name('native_assemble.sh')
    if not script.is_file() or script.is_symlink():
        raise RuntimeError('native assembler missing')
    if output.parent.is_symlink():
        raise RuntimeError('linked output parent')
    output.mkdir(mode=0o700)
    argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
            '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
            '--ro-bind', str(root), '/', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--dir', '/inputs', '--dir', '/construction',
            '--ro-bind', str(inputs), '/inputs', '--ro-bind', str(script), '/native_assemble.sh',
            '--bind', str(output), '/construction', '--clearenv',
            '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LC_ALL', 'C',
            '--', '/usr/bin/bash', '/native_assemble.sh',
            '/inputs/scripts/plan.tsv', '/construction/root', '/construction/work']
    # No host binaries or workspaces are mounted into the isolated root.
    with (output / 'execution.log').open('x') as stream:
        result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError('isolated native construction failed; inspect execution.log')
    status = output / 'work' / 'construction.status'
    if not status.is_file() or status.read_text().strip() != 'assembled-unverified':
        raise RuntimeError('native assembler did not report completed assembly')
    return output


def main():
    parser = argparse.ArgumentParser()
    for arg in ('root', 'archive', 'evidence', 'inputs', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    args = parser.parse_args()
    print(execute(args.root, args.archive, args.evidence, args.inputs, args.output))


if __name__ == '__main__':
    main()
