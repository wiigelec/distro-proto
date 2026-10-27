#!/usr/bin/env python3
"""Cross-build a manifest package with the imported Distro toolchain into root/.

Temporary bridge before the chroot has its own native C compiler and make.
Not a native-chroot build: host utilities run in an isolated cross-build sandbox.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'temporary-tools-manifest.json'

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for buf in iter(lambda: f.read(1024 * 1024), b''):
            h.update(buf)
    return h.hexdigest()

def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, sort_keys=True, indent=2) + '\n')
    os.replace(tmp, path)

def run(cmd, log, timeout=7200):
    """Stream merged build output live and retain a complete package log.

    Use binary chunks rather than line iteration so progress without newline
    is visible immediately, and preserve the byte stream in the log.
    """
    with log.open('ab') as record:
        record.write(('STEP ' + ' '.join(cmd) + '\n').encode())
        record.flush()
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0)
        started = time.monotonic()
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                # Select keeps the timeout effective even if a build emits no output.
                import select
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                if not ready:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                record.write(chunk)
                record.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            remaining = max(0, timeout - (time.monotonic() - started))
            code = process.wait(timeout=remaining)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
    if code:
        raise RuntimeError(f'command failed ({code}); inspect {log}')

def obtain(url, output, expected):
    if output.is_file():
        if sha(output) != expected:
            raise RuntimeError('cached source checksum mismatch: ' + str(output))
        return
    temp = output.with_name(output.name + '.partial')
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temp.open('wb') as f:
            shutil.copyfileobj(response, f)
        if sha(temp) != expected:
            raise RuntimeError('downloaded source checksum mismatch: ' + str(temp))
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)

def extract(archive, destination, topdir):
    if destination.exists():
        if not (destination / topdir / 'configure').is_file():
            raise RuntimeError('incomplete source tree: ' + str(destination))
        return
    temp = destination.with_name(destination.name + '.partial')
    if temp.exists():
        raise RuntimeError('incomplete extraction: ' + str(temp))
    temp.mkdir(parents=True)
    try:
        with tarfile.open(archive, 'r:xz') as tf:
            for m in tf.getmembers():
                p = Path(m.name)
                if p.is_absolute() or not p.parts or p.parts[0] != topdir or '..' in p.parts:
                    raise RuntimeError('unsafe archive path: ' + m.name)
                if not (m.isdir() or m.isfile() or m.issym()):
                    raise RuntimeError('unsupported archive entry: ' + m.name)
            tf.extractall(temp, filter='data')
        if not (temp / topdir / 'configure').is_file():
            raise RuntimeError('configure missing after extraction')
        temp.rename(destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp)

def sandbox(root, work, source_dir):
    cmd = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
           '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
           '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/run',
           '--ro-bind', str(root), '/toolchain', '--bind', str(work), '/work',
           '--ro-bind', str(source_dir), '/work/src',
           '--chdir', '/work/build', '--clearenv',
           '--setenv', 'HOME', '/work', '--setenv', 'LC_ALL', 'C',
           '--setenv', 'PATH', '/toolchain/usr/bin:/usr/bin:/bin']
    for name in ('usr', 'bin', 'sbin', 'lib', 'lib64', 'etc'):
        if (Path('/') / name).exists():
            cmd += ['--ro-bind', '/' + name, '/' + name]
    return cmd + ['--']

def normalize_merged_sbin(stage):
    """Relocate legacy /sbin stage entries under /usr/sbin, without overwrites.

    The stage is scratch output, not the imported cross-toolchain root.
    Reject mixed aliases and any non-directory source rather than merging
    ambiguous package ownership. Run before collision and stamp checks.
    """
    old = stage / 'sbin'
    if not old.exists() and not old.is_symlink():
        return
    if old.is_symlink() or not old.is_dir():
        raise RuntimeError('unsafe staged /sbin')
    usr = stage / 'usr'
    if usr.is_symlink() or not usr.is_dir():
        raise RuntimeError('staged /usr missing or unsafe')
    new = usr / 'sbin'
    if new.is_symlink() or (new.exists() and not new.is_dir()):
        raise RuntimeError('unsafe staged /usr/sbin')
    if not new.exists():
        old.rename(new)
        return
    # Both directories are legitimate glibc outputs. Preflight every entry
    # before mutating the scratch stage; directory overlap is permissible,
    # but file/symlink collisions or traversals through links are not.
    entries = sorted(old.rglob('*'), key=lambda p: (len(p.relative_to(old).parts), str(p)))
    for source in entries:
        target = new / source.relative_to(old)
        if source.is_symlink():
            kind = 'leaf'
        elif source.is_dir():
            kind = 'dir'
        elif source.is_file():
            kind = 'leaf'
        else:
            raise RuntimeError('unsupported staged /sbin entry: ' + str(source))
        parent = target.parent
        while parent != new:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise RuntimeError('unsafe staged /usr/sbin ancestor: ' + str(parent))
            parent = parent.parent
        if kind == 'dir':
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise RuntimeError('staged /sbin directory conflict: ' + str(target))
        elif target.exists() or target.is_symlink():
            raise RuntimeError('staged /sbin file collision: ' + str(target))
    for source in entries:
        target = new / source.relative_to(old)
        if source.is_dir() and not source.is_symlink():
            target.mkdir(exist_ok=True)
        else:
            source.rename(target)
    for source in sorted((p for p in entries if p.is_dir() and not p.is_symlink()),
                         key=lambda p: len(p.parts), reverse=True):
        source.rmdir()
    old.rmdir()


def ensure_merged_sbin(root):
    """Add /sbin -> usr/sbin only if root has no conflicting prior owner."""
    alias = root / 'sbin'
    if alias.is_symlink():
        if os.readlink(alias) != 'usr/sbin':
            raise RuntimeError('incorrect merged-/usr /sbin link')
        return
    if alias.exists():
        raise RuntimeError('non-merged imported /sbin requires verified migration')
    destination = root / 'usr/sbin'
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError('missing or unsafe /usr/sbin for merged link')
    alias.symlink_to('usr/sbin')


def install(stage, root):
    """Preflight staged paths and reject overwrites; then copy selected payload."""
    src = stage / 'usr'
    if not src.is_dir() or src.is_symlink():
        raise RuntimeError('staged /usr missing or unsafe')
    # Filesystem-layout policy is independent of package inventory.
    for top in stage.iterdir():
        if top.name not in ('usr', 'etc') or not top.is_dir() or top.is_symlink():
            raise RuntimeError('unsupported staged top-level path: ' + top.name)
    entries = []
    for path in sorted(stage.rglob('*')):
        if path == stage / 'usr' or path == stage / 'etc':
            continue
        rel = path.relative_to(stage)
        if rel.as_posix() == 'usr/share/info/dir':
            continue
        dst = root / rel
        if path.is_symlink():
            target = os.readlink(path)
            if os.path.isabs(target):
                raise RuntimeError('absolute staged symlink: ' + str(rel))
            # A staged link must resolve within the final root.
            if not (dst.parent / target).resolve(strict=False).is_relative_to(root.resolve()):
                raise RuntimeError('escaping staged symlink: ' + str(rel))
            kind = 'link'
        elif path.is_dir():
            kind = 'dir'
        elif path.is_file():
            kind = 'file'
        else:
            raise RuntimeError('unsupported staged entry: ' + str(rel))
        parent = dst.parent
        while parent != root:
            if parent.is_symlink():
                raise RuntimeError('symlink in install path: ' + str(parent))
            parent = parent.parent
        if kind == 'dir':
            if dst.is_symlink() or (dst.exists() and not dst.is_dir()):
                raise RuntimeError('install directory conflict: ' + str(rel))
        elif dst.exists() or dst.is_symlink():
            raise RuntimeError('install file collision: ' + str(rel))
        entries.append((path, dst, kind, rel))
    created = []
    evidence = {}
    try:
        for source, dst, kind, rel in entries:
            if kind == 'dir':
                if not dst.exists():
                    dst.mkdir(parents=True)
                    created.append(dst)
            elif kind == 'link':
                dst.parent.mkdir(parents=True, exist_ok=True)
                target = os.readlink(source)
                dst.symlink_to(target)
                created.append(dst)
                evidence[str(rel)] = {'link': target}
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dst)
                created.append(dst)
                evidence[str(rel)] = {'sha256': sha(dst)}
    except Exception:
        for p in reversed(created):
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        raise
    return evidence

def main(project, package_name):
    if os.geteuid() == 0:
        raise RuntimeError('run as regular user')
    project = Path(project).expanduser().resolve()
    root, state = project / 'root', project / '.distro-bootstrap'
    if root.is_symlink() or state.is_symlink() or not root.is_dir() or not state.is_dir():
        raise RuntimeError('invalid target root or state')
    if not (root / 'usr/bin/bash').is_file():
        raise RuntimeError('run Bash temp-tools bootstrap first')
    if not (state / 'temp-tools-import.json').is_file():
        raise RuntimeError('verified cross-toolchain import marker missing')
    cfg = json.loads(MANIFEST.read_text())
    if cfg.get('phase') != 'temp-tools' or cfg.get('target') != 'x86_64-distro-linux-gnu':
        raise RuntimeError('invalid temp-tools manifest')
    matches = [p for p in cfg['packages'] if p['name'] == package_name]
    if len(matches) != 1 or matches[0].get('state') != 'executable':
        raise RuntimeError('package manifest entry missing or not executable')
    pkg = matches[0]
    recipe_path = (HERE / pkg['recipe']).resolve()
    if not recipe_path.is_relative_to(HERE) or not recipe_path.is_file():
        raise RuntimeError('invalid package recipe reference')
    recipe = json.loads(recipe_path.read_text())
    if recipe.get('name') != package_name or recipe.get('phase') != 'temp-tools' or recipe.get('execution') != 'host-cross':
        raise RuntimeError('package recipe identity mismatch')
    version = pkg['version']
    source = pkg['source']
    if not re.fullmatch('[0-9a-f]{64}', source['sha256']):
        raise RuntimeError('unpinned source')
    if source['url'] != f'https://ftp.gnu.org/gnu/{package_name}/{package_name}-{version}.tar.xz':
        raise RuntimeError('unapproved source URL')
    stamp = state / 'stamps' / (package_name + '.json')
    recipe_digest = hashlib.sha256((sha(recipe_path) + ':' + json.dumps(pkg, sort_keys=True)).encode()).hexdigest()
    if stamp.is_file():
        record = json.loads(stamp.read_text())
        if record.get('manifest_sha256') != recipe_digest or record.get('status') != 'complete':
            raise RuntimeError('package stamp inconsistent; use clean target for changed inputs')
        for relative, proof in record['installed'].items():
            dest = root / relative
            if 'sha256' in proof and (not dest.is_file() or sha(dest) != proof['sha256']):
                raise RuntimeError('package installed file changed: ' + relative)
            if 'link' in proof and (not dest.is_symlink() or os.readlink(dest) != proof['link']):
                raise RuntimeError('package symlink changed: ' + relative)
        print(f'[temp-tools] {package_name}: SKIP verified stamp', flush=True)
        return
    for binary in recipe['validation']['chroot_executables']:
        if (root / binary.lstrip('/')).exists():
            raise RuntimeError('untracked package executable exists: ' + binary)
    cache = state / 'sources'
    cache.mkdir(exist_ok=True)
    archive = cache / f'{package_name}-{version}.tar.xz'
    print(f'[temp-tools] {package_name}: downloading and verifying source', flush=True)
    obtain(source['url'], archive, source['sha256'])
    extracted = cache / f'extracted-{package_name}-{version}'
    extract(archive, extracted, f'{package_name}-{version}')
    run_root = state / 'work' / package_name
    run_root.mkdir(parents=True, exist_ok=True)
    number = 1
    while (run_root / f'run-{number:03d}').exists():
        number += 1
    work = run_root / f'run-{number:03d}'
    (work / 'build').mkdir(parents=True)
    (work / 'stage').mkdir()
    (work / 'src').mkdir()
    log = work / 'build.log'
    target = cfg['target']
    cc = f'/toolchain/usr/bin/{target}-gcc --sysroot=/toolchain -fno-link-libatomic'
    guest = sandbox(root, work, extracted / f'{package_name}-{version}')
    variables = {'target': target, 'version': version, 'jobs': str(min(os.cpu_count() or 2, 8)),
                 'cc': cc, 'ldflags': '-B/toolchain/usr/lib64/ -L/toolchain/usr/lib64 -L/toolchain/lib64'}
    print(f'[temp-tools] {package_name}: executing manifest recipe with target cross compiler', flush=True)
    for step in recipe['steps']:
        if not isinstance(step, dict) or set(step) != {'name', 'argv'}:
            raise RuntimeError('invalid package recipe step')
        argv = [arg.format_map(variables) for arg in step['argv']]
        if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise RuntimeError('invalid package recipe argv')
        print(f'[temp-tools] {package_name}: step=' + step['name'], flush=True)
        run(guest + argv, log)
    installed = install(work / 'stage', root)
    for binary in recipe['validation']['chroot_executables']:
        if not (root / binary.lstrip('/')).is_file():
            raise RuntimeError('staged package executable missing: ' + binary)
    for binary in recipe['validation']['chroot_executables']:
        verify = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
                  '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
                  '--bind', str(root), '/', '--proc', '/proc', '--dev', '/dev',
                  '--tmpfs', '/tmp', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                  '--', binary, '--version']
        run(verify, log, timeout=90)
    save(stamp, {'schema_version': 1, 'phase': 'temp-tools', 'package': package_name,
                 'status': 'complete', 'manifest_sha256': recipe_digest,
                 'source_sha256': sha(archive), 'installed': installed})
    print(f'[temp-tools] {package_name}: COMPLETE; {len(installed)} files and links; binaries verified in chroot', flush=True)

if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise RuntimeError('usage: temp_cross_package.py PROJECT PACKAGE')
        main(sys.argv[1], sys.argv[2])
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.TimeoutExpired) as e:
        print('[temp-tools] cross-package ERROR:', e, file=sys.stderr, flush=True)
        sys.exit(1)
