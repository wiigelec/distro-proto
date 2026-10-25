"""Temporary-tools archive self-hosting proof. Never installs into the imported root."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import urllib.request

from temporary_tools import package_runner
from temporary_tools import temp_tools_bootstrap as bootstrap


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n')
    os.replace(temp, path)


def archive_inventory(archive):
    result = {}
    with tarfile.open(archive, 'r:xz') as tf:
        members = bootstrap.archive_members(tf)
        for m in members:
            if m.isfile():
                f = tf.extractfile(m)
                if f is None:
                    raise RuntimeError('unreadable member ' + m.name)
                h = hashlib.sha256()
                for block in iter(lambda: f.read(1048576), b''):
                    h.update(block)
                result[m.name] = ('file', h.hexdigest())
            elif m.issym():
                result[m.name] = ('symlink', m.linkname)
            elif m.islnk():
                result[m.name] = ('hardlink', m.linkname)
            elif m.isdir():
                result[m.name] = ('dir', None)
    return result


def verify_extracted(root, inventory):
    for rel, (kind, proof) in inventory.items():
        p = root / rel
        if kind == 'file' and (not p.is_file() or p.is_symlink() or digest(p) != proof):
            raise RuntimeError('imported file differs: ' + rel)
        if kind == 'symlink' and (not p.is_symlink() or os.readlink(p) != proof):
            raise RuntimeError('imported symlink differs: ' + rel)
        if kind == 'hardlink':
            target = root / proof
            if (p.is_symlink() or not p.is_file() or target.is_symlink() or
                    not target.is_file() or not os.path.samefile(p, target)):
                raise RuntimeError('imported hardlink differs: ' + rel)
        if kind == 'dir' and (not p.is_dir() or p.is_symlink()):
            raise RuntimeError('imported directory differs: ' + rel)
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*')}
    if actual != set(inventory):
        raise RuntimeError('imported archive member inventory differs')


def safe_source_archive(tf, top):
    for m in tf.getmembers():
        p = Path(m.name)
        if (p.is_absolute() or not p.parts or p.parts[0] != top or '..' in p.parts
                or not (m.isfile() or m.isdir() or m.issym())):
            raise RuntimeError('unsafe package source member: ' + m.name)


def acquire_source(state, source, filename, algorithm='sha256'):
    """Keep all source inputs in the validation workspace; verify fixed digests."""
    expected = source[algorithm]
    if not re.fullmatch('[0-9a-f]{64}' if algorithm == 'sha256' else '[0-9a-f]{128}', expected):
        raise RuntimeError('invalid pinned source digest')
    url = source['url']
    if (not url.startswith(('https://ftp.gnu.org/gnu/',
                            'https://gcc.gnu.org/pub/gcc/releases/',
                            'https://gcc.gnu.org/pub/gcc/infrastructure/'))
            or url.rsplit('/', 1)[-1] != filename):
        raise RuntimeError('unapproved pinned source URL')
    cache = state / 'sources'
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / filename
    def verified(path):
        h = hashlib.new(algorithm)
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1048576), b''):
                h.update(block)
        return h.hexdigest() == expected
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or not verified(destination):
            raise RuntimeError('cached pinned source changed: ' + filename)
        return destination
    partial = cache / (filename + '.partial')
    if partial.exists():
        raise RuntimeError('incomplete pinned source download: ' + filename)
    try:
        print('[native-verify] Downloading pinned source: ' + filename, flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, partial.open('xb') as output:
            downloaded = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                downloaded += len(block)
                if downloaded // (16 * 1024 * 1024) != (downloaded - len(block)) // (16 * 1024 * 1024):
                    print(f'[native-verify] {filename}: downloaded {downloaded // (1024 * 1024)} MiB', flush=True)
        if not verified(partial):
            raise RuntimeError('downloaded source digest mismatch: ' + filename)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def build_script(name, version, recipe, source, prereqs):
    steps = recipe.get('native_steps')
    # Product host-cross recipes may be reused only after native-only variables
    # and sysroot flags have been removed by the existing vetted translator.
    explicit = steps is not None
    steps = steps if explicit else recipe['steps']
    values = {'name': name, 'version': version, 'target': 'x86_64-distro-linux-gnu', 'jobs': '2'}
    lines = ['#!/bin/bash', 'set -euo pipefail', 'umask 022',
             'export PATH=/usr/bin:/bin', 'export LC_ALL=C', 'export HOME=/build',
             'mkdir -p /build/build /build/stage', 'cd /build/build']
    for dep in prereqs:
        lines.append('ln -s ' + shlex.quote('/prerequisites/' + dep) + ' ' + shlex.quote(str(source / dep)))
    for item in steps:
        argv = package_runner.native_recipe_argv(item, values, explicit_native=explicit)
        # Translation supports native script paths in /work/native; map the
        # generated guest workspace to the read-only /sources and /build mounts.
        fixed = []
        for arg in argv:
            arg = arg.replace('/work/src', str(source))
            arg = arg.replace('/work/native/' + name + '/src', str(source))
            arg = arg.replace('/work/native/' + name + '/stage', '/build/stage')
            if '/toolchain' in arg or '--sysroot=' in arg:
                raise RuntimeError('cross-only argument in native script: ' + name + ': ' + arg)
            fixed.append(arg)
        if not fixed or not all(isinstance(a, str) and a and '\n' not in a for a in fixed):
            raise RuntimeError('invalid native step: ' + name)
        lines += ['echo ' + shlex.quote('STEP ' + item['name']), shlex.join(fixed)]
    lines.append('test -d /build/stage/usr')
    lines.append('echo ' + shlex.quote('NATIVE-BUILD-PASS ' + name))
    return '\n'.join(lines) + '\n'


def verify_stage(stage):
    files = {}
    base = stage / 'usr'
    if not base.is_dir():
        raise RuntimeError('stage-install did not produce /usr')
    for path in sorted(base.rglob('*')):
        rel = path.relative_to(stage).as_posix()
        if path.is_symlink():
            files[rel] = {'link': os.readlink(path)}
        elif path.is_file():
            files[rel] = {'sha256': digest(path)}
    if not files:
        raise RuntimeError('native stage has no files')
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, required=True, help='Fresh validation workspace')
    ap.add_argument('--temp-tools', type=Path, required=True, help='Verified temporary-tools archive')
    args = ap.parse_args(argv)
    if os.geteuid() == 0:
        ap.error('run unprivileged')
    project = args.root.expanduser().resolve()
    archive = args.temp_tools.expanduser().resolve(strict=True)
    if project in (Path('/'), Path.home().resolve()) or archive.is_relative_to(project):
        raise RuntimeError('validation workspace must be separate from temporary-tools archive')
    if archive.name != 'temp-tools-root.tar.xz':
        raise RuntimeError('expected temporary-tools archive filename')
    evidence_path = archive.parent / 'temp-tools-evidence.json'
    evidence = json.loads(evidence_path.read_text())
    if evidence.get('status') != 'verified' and evidence.get('status') != 'passed':
        raise RuntimeError('temporary-tools export evidence not verified')
    # The evidence schema is authoritative; explicitly refuse unknown formats.
    print('[native-verify] Verifying temporary-tools archive SHA-256: ' + str(archive), flush=True)
    observed = digest(archive)
    sha = evidence.get('handoff', {}).get('sha256')
    if sha != observed:
        raise RuntimeError('temporary-tools archive digest not bound to export evidence')
    print('[native-verify] Archive checksum verified; scanning members', flush=True)
    inventory = archive_inventory(archive)
    if (evidence.get('target') != 'x86_64-distro-linux-gnu' or
            evidence.get('handoff', {}).get('members') != len(inventory)):
        raise RuntimeError('temporary-tools export identity or inventory mismatch')
    print(f'[native-verify] Verified {len(inventory)} archive members', flush=True)
    root, state = project / 'root', project / '.distro-bootstrap'
    if project.is_symlink() or root.is_symlink() or state.is_symlink():
        raise RuntimeError('symlink validation workspace')
    marker = state / 'import.json'
    if not marker.exists():
        if (root.exists() and any(root.iterdir())) or (state.exists() and any(state.iterdir())):
            raise RuntimeError('fresh validation workspace required')
        root.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        print('[native-verify] Extracting verified archive into isolated root', flush=True)
        with tarfile.open(archive, 'r:xz') as tf:
            members = bootstrap.archive_members(tf)
            for index, member in enumerate(members, 1):
                tf.extract(member, root, filter='data')
                if index % 250 == 0 or index == len(members):
                    print(f'[native-verify] Extracted {index}/{len(members)} members', flush=True)
        print('[native-verify] Checking extracted root against archive inventory', flush=True)
        verify_extracted(root, inventory)
        write_json(marker, {'archive_sha256': observed, 'member_count': len(inventory)})
    elif json.loads(marker.read_text()) != {'archive_sha256': observed, 'member_count': len(inventory)}:
        raise RuntimeError('validation root import marker mismatch')
    print('[native-verify] Verifying imported root contents', flush=True)
    verify_extracted(root, inventory)
    print('[native-verify] Imported root verified; checking package manifest', flush=True)
    manifest = json.loads(package_runner.MANIFEST.read_text())
    if len(manifest['packages']) != 10:
        raise RuntimeError('unexpected temporary-tools package closure')
    package_names = [p['name'] for p in manifest['packages']]
    report = {'schema_version': 1, 'status': 'incomplete', 'archive_sha256': observed,
              'packages': {}, 'member_count': len(inventory)}
    for pkg in manifest['packages']:
        name, version = pkg['name'], pkg['version']
        print('[native-verify] ' + name, flush=True)
        source = pkg['source']
        filename = source['url'].rsplit('/', 1)[-1]
        print('[native-verify] ' + name + ': checking pinned source ' + filename, flush=True)
        src_archive = acquire_source(state, source, filename)
        print('[native-verify] ' + name + ': source SHA-256 verified', flush=True)
        recipe_path = package_runner.HERE / pkg['recipe']
        recipe = json.loads(recipe_path.read_text())
        stamp = state / 'native-stamps' / (name + '.json')
        run = state / 'work' / name
        if stamp.exists():
            proof = json.loads(stamp.read_text())
            if (proof.get('archive_sha256') != observed or proof.get('source_sha256') != source['sha256']
                    or proof.get('recipe_sha256') != digest(recipe_path)
                    or proof.get('status') != 'passed'
                    or not (run / 'build.sh').is_file()
                    or digest(run / 'build.sh') != proof.get('script_sha256')):
                raise RuntimeError('stale validation stamp: ' + name)
            for rel, p in proof['staged'].items():
                f = run / 'stage' / rel
                if 'sha256' in p and (not f.is_file() or digest(f) != p['sha256']):
                    raise RuntimeError('modified native stage: ' + rel)
                if 'link' in p and (not f.is_symlink() or os.readlink(f) != p['link']):
                    raise RuntimeError('modified native link: ' + rel)
            report['packages'][name] = proof
            continue
        if run.exists():
            raise RuntimeError('uncommitted native validation run: ' + str(run))
        run.mkdir(parents=True)
        (run / 'build').mkdir()
        (run / 'stage').mkdir()
        top = pkg.get('upstream_name', name) + '-' + version
        source_dir = run / 'src' / top
        (run / 'src').mkdir()
        with tarfile.open(src_archive, 'r:*') as tf:
            safe_source_archive(tf, top)
            tf.extractall(run / 'src', filter='data')
        prereqs = []
        dep_paths = {}
        if pkg.get('build_prerequisites'):
            for dep in pkg['build_prerequisites']:
                fname = dep['url'].rsplit('/', 1)[-1]
                dep_archive = acquire_source(state, dep, fname, algorithm='sha512')
                dep_top = dep['name'] + '-' + dep['version']
                dst = run / 'deps' / dep['name']
                dst.parent.mkdir(exist_ok=True)
                with tarfile.open(dep_archive, 'r:*') as tf:
                    safe_source_archive(tf, dep_top)
                    tf.extractall(dst.parent, filter='data')
                dep_paths[dep['name']] = dst.parent / dep_top
                prereqs.append(dep['name'])
        # GCC needs its read-only source overlay to contain gmp/mpfr/mpc;
        # build the overlay in the private writable work tree, not the imported temporary-tools root.
        if prereqs:
            for dep in prereqs:
                if (source_dir / dep).exists() or (source_dir / dep).is_symlink():
                    raise RuntimeError('GCC prerequisite path already exists: ' + dep)
                (source_dir / dep).symlink_to('/prerequisites/' + dep)
        text = build_script(name, version, recipe, Path('/sources') / top, [])
        script = run / 'build.sh'
        script.write_text(text)
        script.chmod(0o644)
        if shutil.which('bwrap') is None:
            raise RuntimeError('bubblewrap required')
        cmd = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
               '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
               '--ro-bind', str(root), '/', '--bind', str(run), '/build',
               '--dir', '/sources', '--ro-bind', str(source_dir), '/sources/' + top,
               '--dir', '/prerequisites', '--proc', '/proc', '--dev', '/dev',
               '--tmpfs', '/tmp', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
               '--setenv', 'HOME', '/build', '--setenv', 'LC_ALL', 'C']
        for dep, dep_src in dep_paths.items():
            cmd += ['--ro-bind', str(dep_src), '/prerequisites/' + dep]
        cmd += ['--', '/bin/bash', '--noprofile', '--norc', '/build/build.sh']
        log = run / 'build.log'
        print(f'[native-verify] {name}: streaming build output; log={log}', flush=True)
        # Use the same binary-chunk live tee as the existing temporary-tools package runner.
        # Failure retains the complete log and aborts before stamping success.
        package_runner.common.run(cmd, log, timeout=7200)
        staged = verify_stage(run / 'stage')
        proof = {'archive_sha256': observed, 'source_sha256': source['sha256'],
                 'recipe_sha256': digest(recipe_path), 'script_sha256': digest(script),
                 'staged': staged, 'status': 'passed'}
        write_json(stamp, proof)
        report['packages'][name] = proof
    report['status'] = 'passed'
    write_json(project / 'artifacts/temp-tools-native-validation.json', report)
    print('[native-verify] PASS: ' + ', '.join(package_names), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
