#!/usr/bin/env python3
"""Single manifest/recipe interpreter for Distro temporary-tools cross builds.

Host build utilities may run in the build namespace; only cross-compiled,
verified staged artifacts enter the target root. No package-name dispatch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from . import temp_cross_package as common
from . import temp_tools_bootstrap as bootstrap

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'temporary-tools-manifest.json'


def safe_source(pkg, state):
    source = pkg['source']
    upstream = pkg.get('upstream_name', pkg['name'])
    filename = source['url'].rsplit('/', 1)[-1]
    if filename not in (f'{upstream}-{pkg["version"]}.tar.xz',
                        f'{upstream}-{pkg["version"]}.tar.gz'):
        raise RuntimeError('source archive filename differs from package identity')
    if not source['url'].startswith(('https://ftp.gnu.org/gnu/',
                                     'https://gcc.gnu.org/pub/gcc/releases/')):
        raise RuntimeError('source origin not approved: ' + source['url'])
    if not re.fullmatch(r'[a-f0-9]{64}', source['sha256']):
        raise RuntimeError('source not SHA256-pinned')
    cache = state / 'sources'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / filename
    common.obtain(source['url'], archive, source['sha256'])
    extracted = cache / f'extracted-{upstream}-{pkg["version"]}'
    top = f'{upstream}-{pkg["version"]}'
    if not extracted.exists():
        temporary = extracted.with_name(extracted.name + '.partial')
        if temporary.exists():
            raise RuntimeError('partial extraction left behind: ' + str(temporary))
        temporary.mkdir()
        try:
            with tarfile.open(archive, 'r:*') as tf:
                for entry in tf.getmembers():
                    path = Path(entry.name)
                    if (path.is_absolute() or not path.parts or path.parts[0] != top
                            or '..' in path.parts or not
                            (entry.isdir() or entry.isfile() or entry.issym())):
                        raise RuntimeError('unsafe source member: ' + entry.name)
                tf.extractall(temporary, filter='data')
            if not (temporary / top / 'configure').is_file():
                raise RuntimeError('source configure missing')
            temporary.rename(extracted)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    source_dir = extracted / top
    if not (source_dir / 'configure').is_file():
        raise RuntimeError('source cache is incomplete')
    return source_dir, archive


def sandbox_for(root, work, source_dir, pkg, state):
    """Generic source-overlay capability for compilers with in-tree prerequisites."""
    dependencies = pkg.get('build_prerequisites', [])
    if not dependencies:
        return common.sandbox(root, work, source_dir)
    overlay = work / 'source-overlay'
    overlay.mkdir()
    for entry in source_dir.iterdir():
        if entry.name in {item['name'] for item in dependencies}:
            raise RuntimeError('source prerequisite conflicts with source tree')
        (overlay / entry.name).symlink_to('/upstream/' + entry.name)
    mounts = []
    for item in dependencies:
        name, version = item['name'], item['version']
        filename = item['url'].rsplit('/', 1)[-1]
        if (not re.fullmatch(r'[a-z0-9]+', name) or
                filename not in (f'{name}-{version}.tar.gz', f'{name}-{version}.tar.bz2') or
                item['url'] != f'https://gcc.gnu.org/pub/gcc/infrastructure/{filename}' or
                not re.fullmatch(r'[a-f0-9]{128}', item['sha512'])):
            raise RuntimeError('invalid pinned source prerequisite')
        archive = state / 'sources' / filename
        if archive.exists():
            if hashlib.sha512(archive.read_bytes()).hexdigest() != item['sha512']:
                raise RuntimeError('prerequisite checksum mismatch: ' + name)
        else:
            import urllib.request
            temp = archive.with_name(archive.name + '.partial')
            try:
                with urllib.request.urlopen(item['url'], timeout=120) as response, temp.open('wb') as out:
                    shutil.copyfileobj(response, out)
                if hashlib.sha512(temp.read_bytes()).hexdigest() != item['sha512']:
                    raise RuntimeError('prerequisite source checksum mismatch: ' + name)
                os.replace(temp, archive)
            finally:
                temp.unlink(missing_ok=True)
        destination = state / 'sources' / f'prerequisite-{name}-{version}'
        if not destination.exists():
            temporary = destination.with_name(destination.name + '.partial')
            if temporary.exists():
                raise RuntimeError('partial prerequisite extraction')
            temporary.mkdir()
            try:
                with tarfile.open(archive, 'r:*') as tf:
                    for entry in tf.getmembers():
                        p = Path(entry.name)
                        if (p.is_absolute() or not p.parts or p.parts[0] != f'{name}-{version}'
                                or '..' in p.parts or not
                                (entry.isdir() or entry.isfile() or entry.issym())):
                            raise RuntimeError('unsafe prerequisite archive member')
                    tf.extractall(temporary, filter='data')
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        src = destination / f'{name}-{version}'
        if not (src / 'configure').is_file():
            raise RuntimeError('incomplete prerequisite source: ' + name)
        (overlay / name).symlink_to('/prerequisites/' + name)
        mounts.extend(['--ro-bind', str(src), '/prerequisites/' + name])
    guest = common.sandbox(root, work, overlay)
    return guest[:-1] + ['--ro-bind', str(source_dir), '/upstream',
                         '--dir', '/prerequisites'] + mounts + ['--']


def verify_stamp(stamp, signature, root):
    record = json.loads(stamp.read_text())
    if record.get('status') != 'complete' or record.get('manifest_sha256') != signature:
        raise RuntimeError('stale or inconsistent package stamp: ' + stamp.stem)
    if not record.get('installed'):
        raise RuntimeError('empty completed package stamp')
    for rel, evidence in record['installed'].items():
        dest = root / rel
        if ('sha256' in evidence and (dest.is_symlink() or not dest.is_file()
                                     or common.sha(dest) != evidence['sha256'])) or (
                'link' in evidence and (not dest.is_symlink()
                                        or os.readlink(dest) != evidence['link'])):
            raise RuntimeError('installed file differs from stamp: ' + rel)


def target_test(root, work, binary):
    guest = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
             '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
             '--bind', str(root), '/', '--bind', str(work), '/work',
             '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
             '--setenv', 'LC_ALL', 'C', '--']
    proc = subprocess.run(guest + [binary, '--version'], capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError('isolated target execution failed: ' + binary + '\n' + proc.stderr[-1200:])


def native_compiler_smoke(root, work):
    (work / 'probe.c').write_text('#include <stdio.h>\nint main(void) { puts("distro-native-gcc-ok"); return 0; }\n')
    guest = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
             '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
             '--bind', str(root), '/', '--bind', str(work), '/work',
             '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LC_ALL', 'C', '--']
    common.run(guest + ['/usr/bin/gcc', '/work/probe.c', '-o', '/work/probe'],
               work / 'compiler-smoke.log', timeout=240)
    p = subprocess.run(guest + ['/work/probe'], capture_output=True, text=True, timeout=90)
    if p.returncode or p.stdout.strip() != 'distro-native-gcc-ok':
        raise RuntimeError('native compiler smoke test failed: ' + p.stderr[-1200:])


def install_handoff(stage, root, policy, pkg, manifest, cross_archive):
    """Guarded transfer of compiler-private backends; no historical run assumptions."""
    prefix = f'usr/libexec/gcc/{manifest["target"]}/{pkg["version"]}/'
    source_root = stage / 'usr'
    if not source_root.is_dir():
        raise RuntimeError('missing staged usr')
    allowed = policy['compiler_private_prefix']
    if allowed != 'manifest-target-and-version':
        raise RuntimeError('unrecognized ownership handoff policy')
    # Archive-owned collisions outside the compiler-private directory are
    # preserved, never replaced. Compiler-private collisions are transferred
    # only after byte-for-byte verification against the imported archive.
    marker = json.loads((root.parent / '.distro-bootstrap/temp-tools-import.json').read_text())
    archive = Path(cross_archive).expanduser().resolve(strict=True)
    if common.sha(archive) != marker['cross_archive_sha256']:
        raise RuntimeError('cross archive differs from verified import')
    colliding = []
    for path in sorted(source_root.rglob('*')):
        rel = path.relative_to(stage).as_posix()
        dest = root / rel
        if rel == 'usr/share/info/dir':
            continue
        if dest.exists() or dest.is_symlink():
            if path.is_dir() and not path.is_symlink() and dest.is_dir() and not dest.is_symlink():
                continue
            colliding.append((path, dest, rel))
    required = {rel for _, _, rel in colliding}
    expected = {}
    with tarfile.open(archive, 'r:xz') as tf:
        for member in tf:
            rel = member.name.removeprefix('./')
            if rel not in required:
                continue
            if member.isfile():
                src = tf.extractfile(member)
                if src is None:
                    raise RuntimeError('missing imported archive file: ' + rel)
                digest = hashlib.sha256()
                for block in iter(lambda: src.read(1024 * 1024), b''):
                    digest.update(block)
                expected[rel] = ('sha256', digest.hexdigest())
            elif member.issym():
                expected[rel] = ('link', member.linkname)
            elif member.islnk():
                # Tar hard links are physical files in the imported root. Only
                # accept links to an earlier regular archive member, as required
                # by the cross-toolchain import validator.
                target = member.linkname
                target_path = Path(target)
                if (target_path.is_absolute() or '..' in target_path.parts
                        or not target_path.parts or target == rel):
                    raise RuntimeError('unsafe imported archive hard link: ' + rel)
                try:
                    owner = tf.getmember(target)
                except KeyError as exc:
                    raise RuntimeError('missing imported archive hard link target: ' + rel) from exc
                if not owner.isfile() or owner.offset >= member.offset:
                    raise RuntimeError('invalid imported archive hard link target: ' + rel)
                src = tf.extractfile(owner)
                if src is None:
                    raise RuntimeError('unreadable imported archive hard link target: ' + rel)
                digest = hashlib.sha256()
                for block in iter(lambda: src.read(1024 * 1024), b''):
                    digest.update(block)
                expected[rel] = ('hardlink', (target, digest.hexdigest()))
            else:
                raise RuntimeError('unsupported imported collision: ' + rel)
    transition, preserve = [], []
    for path, dest, rel in colliding:
        if rel not in expected:
            raise RuntimeError('collision not owned by cross-toolchain: ' + rel)
        kind, value = expected[rel]
        if kind == 'sha256' and (not dest.is_file() or dest.is_symlink() or common.sha(dest) != value):
            raise RuntimeError('modified imported file: ' + rel)
        if kind == 'link' and (not dest.is_symlink() or os.readlink(dest) != value):
            raise RuntimeError('modified imported symlink: ' + rel)
        if kind == 'hardlink':
            owner_rel, owner_digest = value
            owner_path = root / owner_rel
            if (dest.is_symlink() or not dest.is_file()
                    or owner_path.is_symlink() or not owner_path.is_file()
                    or not os.path.samefile(dest, owner_path)
                    or common.sha(dest) != owner_digest):
                raise RuntimeError('modified imported hard link: ' + rel)
        if rel.startswith(prefix):
            transition.append((path, dest, rel))
        else:
            preserve.append(path)
    if not any(rel == prefix + 'cc1' for _, _, rel in transition):
        raise RuntimeError('compiler-private cc1 handoff evidence missing')
    # Drop only staged duplicates of verified imported paths. The live imported
    # files remain untouched and are deliberately absent from this package stamp.
    for path in preserve:
        path.unlink()
    # Back up verified imported files, so failed install or validation can restore them.
    backup = []
    for _, dest, rel in transition:
        store = stage.parent / 'backups' / rel
        store.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            store.symlink_to(os.readlink(dest))
        else:
            shutil.copy2(dest, store)
        backup.append((dest, store))
    for dest, _ in backup:
        dest.unlink()
    try:
        evidence = common.install(stage, root)
    except Exception:
        for dest, source in backup:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                dest.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, dest)
        raise
    return evidence, backup


def package_run(project, manifest, pkg, cross_archive):
    root, state = project / 'root', project / '.distro-bootstrap'
    name = pkg['name']
    recipe_path = (HERE / pkg['recipe']).resolve()
    if not recipe_path.is_relative_to(HERE) or not recipe_path.is_file():
        raise RuntimeError('unsafe recipe path')
    recipe = json.loads(recipe_path.read_text())
    if (recipe.get('name') != name or recipe.get('phase') != 'temp-tools'
            or recipe.get('execution') != 'host-cross'):
        raise RuntimeError('recipe identity/execution mismatch: ' + name)
    signature = hashlib.sha256((common.sha(recipe_path) + ':' + json.dumps(pkg, sort_keys=True)).encode()).hexdigest()
    stamp = state / 'stamps' / (name + '.json')
    if stamp.is_file():
        verify_stamp(stamp, signature, root)
        print(f'[temp-tools] {name}: SKIP verified stamp', flush=True)
        return
    validation = recipe['validation']['chroot_executables']
    for binary in validation:
        candidate = root / binary.lstrip('/')
        if candidate.exists() or candidate.is_symlink():
            raise RuntimeError('untracked installed executable: ' + binary)
    source_dir, archive = safe_source(pkg, state)
    runroot = state / 'work' / name
    runroot.mkdir(parents=True, exist_ok=True)
    index = 1
    while (runroot / f'run-{index:03d}').exists():
        index += 1
    work = runroot / f'run-{index:03d}'
    (work / 'build').mkdir(parents=True)
    (work / 'stage').mkdir()
    log = work / 'build.log'
    target = manifest['target']
    values = {'target': target, 'version': pkg['version'],
              'jobs': str(min(os.cpu_count() or 2, 8)),
              'cc': f'/toolchain/usr/bin/{target}-gcc --sysroot=/toolchain -fno-link-libatomic',
              'cxx': f'/toolchain/usr/bin/{target}-g++ --sysroot=/toolchain -fno-link-libatomic',
              'ldflags': '-B/toolchain/usr/lib64/ -L/toolchain/usr/lib64 -L/toolchain/lib64'}
    guest = sandbox_for(root, work, source_dir, pkg, state)
    # Recipe environment is applied to every sandbox command, including
    # recursive configure/build subprocesses. An argv assignment passed to
    # the top-level configure does not reliably reach GCC sub-configures.
    env = recipe.get('environment', {})
    if not isinstance(env, dict):
        raise RuntimeError('invalid recipe environment: ' + name)
    env_args = []
    for key, value in sorted(env.items()):
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                or key in ('PATH', 'HOME', 'LC_ALL') or key.startswith(('LD_', 'BASH_FUNC_'))
                or not isinstance(value, str) or '\x00' in value or '\n' in value):
            raise RuntimeError('unsafe recipe environment declaration: ' + str(key))
        env_args += ['--setenv', key, value.format_map(values)]
    guest = guest[:-1] + env_args + ['--']
    for step in recipe['steps']:
        if not isinstance(step, dict) or set(step) != {'name', 'argv'}:
            raise RuntimeError('invalid recipe step: ' + name)
        argv = [arg.format_map(values) for arg in step['argv']]
        print(f'[temp-tools] {name}: step={step["name"]}', flush=True)
        common.run(guest + argv, log, timeout=7200)
    policy = pkg.get('installation', {})
    backup = []
    if policy:
        evidence, backup = install_handoff(work / 'stage', root, policy, pkg, manifest, cross_archive)
    else:
        evidence = common.install(work / 'stage', root)
    try:
        if not evidence:
            raise RuntimeError('empty package stage: ' + name)
        for rel, target_link in recipe.get('post_install_symlinks', {}).items():
            path = Path(rel)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or '/' in target_link or target_link in ('.', '..')):
                raise RuntimeError('unsafe post-install link declaration')
            dest = root / path
            if not dest.parent.is_dir() or dest.parent.is_symlink():
                raise RuntimeError('post-install symlink parent missing or unsafe')
            if dest.exists() or dest.is_symlink():
                if not dest.is_symlink() or os.readlink(dest) != target_link:
                    raise RuntimeError('post-install symlink collision: ' + rel)
            else:
                dest.symlink_to(target_link)
            evidence[rel] = {'link': target_link}
        for binary in validation:
            if not (root / binary.lstrip('/')).is_file():
                raise RuntimeError('expected target executable absent: ' + binary)
            target_test(root, work, binary)
        if pkg.get('compiler_smoke'):
            native_compiler_smoke(root, work)
    except Exception:
        # Restore only the verified transition files; ordinary install collisions remain fail-closed.
        if backup:
            for rel in evidence:
                dest = root / rel
                if dest.is_file() or dest.is_symlink():
                    dest.unlink()
            for dest, src in backup:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_symlink():
                    dest.symlink_to(os.readlink(src))
                else:
                    shutil.copy2(src, dest)
        raise
    common.save(stamp, {'schema_version': 1, 'phase': 'temp-tools',
                        'package': name, 'status': 'complete',
                        'manifest_sha256': signature, 'source_sha256': common.sha(archive),
                        'installed': evidence})
    print(f'[temp-tools] {name}: COMPLETE; {len(evidence)} files/links verified', flush=True)


def resolve_cross_toolchain_archive(source):
    """Locate and verify the cross-toolchain handoff archive."""
    candidate = Path(source).expanduser().resolve(strict=True)
    if candidate.is_dir():
        options = [candidate / 'artifacts/cross-toolchain-root.tar.xz',
                   candidate / 'cross-toolchain-root.tar.xz']
        matches = [p for p in options if p.is_file()]
        if len(matches) != 1:
            raise RuntimeError('expected exactly one cross-toolchain handoff archive')
        candidate = matches[0]
    if not candidate.is_file() or candidate.name != 'cross-toolchain-root.tar.xz':
        raise RuntimeError('cross-toolchain cross-toolchain-root.tar.xz required')
    reports = [candidate.parent.parent / '.distro-bootstrap/cross-toolchain-evidence.json',
               candidate.parent / 'cross-toolchain-evidence.json']
    report = next((p for p in reports if p.is_file()), None)
    if report is None:
        raise RuntimeError('cross-toolchain verification evidence missing')
    record = json.loads(report.read_text())
    if record.get('status') != 'verified' or record.get('target') != 'x86_64-distro-linux-gnu':
        raise RuntimeError('cross-toolchain verification evidence invalid')
    handoff = record.get('handoff', {})
    print(f'[cross-toolchain] Verifying archive SHA-256: {candidate}', flush=True)
    if handoff.get('sha256') != common.sha(candidate):
        raise RuntimeError('cross-toolchain archive checksum mismatch')
    print('[cross-toolchain] Archive checksum verified', flush=True)
    if Path(handoff.get('archive', '')).name != candidate.name:
        raise RuntimeError('cross-toolchain handoff archive identity mismatch')
    return candidate


def import_cross_toolchain(project, cross_archive, target):
    """Verify and import the cross-toolchain into a clean target root."""
    if project in (Path('/'), Path.home().resolve()):
        raise RuntimeError('unsafe project path')
    archive = resolve_cross_toolchain_archive(cross_archive)
    root, state = project / 'root', project / '.distro-bootstrap'
    print(f'[import] Checking target root: {root}', flush=True)
    bootstrap.check_directory(root)
    bootstrap.check_directory(state)
    print('[import] Checking cross-toolchain archive digest', flush=True)
    digest = common.sha(archive)
    marker = state / 'temp-tools-import.json'
    if marker.exists():
        previous = json.loads(marker.read_text())
        if (previous.get('stage') != 'temp-tools' or previous.get('status') != 'complete'
                or previous.get('cross_archive_sha256') != digest
                or not root.is_dir() or bootstrap.empty(root)):
            raise RuntimeError('inconsistent prior cross-toolchain import')
        for rel, expected in previous.get('files', {}).items():
            if common.sha(root / rel) != expected:
                raise RuntimeError('imported cross-toolchain file changed: ' + rel)
        print('[import] Existing cross-toolchain import verified', flush=True)
        return
    if not bootstrap.empty(root) or not bootstrap.empty(state):
        raise RuntimeError('initial cross-toolchain import requires empty root and state')
    print('[import] Inspecting cross-toolchain archive members', flush=True)
    with tarfile.open(archive, 'r:xz') as tf:
        members = bootstrap.archive_members(tf)
        project.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        total = len(members)
        print(f'[import] Extracting {total} verified archive members', flush=True)
        for index, member in enumerate(members, 1):
            tf.extract(member, root, filter='data')
            if index % 250 == 0 or index == total:
                print(f'[import] Extracted {index}/{total} members', flush=True)
    print('[import] Validating compiler and runtime files', flush=True)
    required = (f'usr/bin/{target}-gcc', f'usr/bin/{target}-g++',
                'lib64/libc.so.6', 'lib64/ld-linux-x86-64.so.2',
                'usr/include/linux/version.h')
    checks = {}
    for rel in required:
        if not (root / rel).is_file():
            raise RuntimeError('imported prerequisite missing: ' + rel)
        checks[rel] = common.sha(root / rel)
    common.save(marker, {'schema_version': 1, 'stage': 'temp-tools',
                         'status': 'complete', 'cross_archive_sha256': digest,
                         'files': checks})
    print('[import] Cross-toolchain import complete', flush=True)


def main(project, cross_archive):
    if os.geteuid() == 0:
        raise RuntimeError('run as ordinary user')
    project = Path(project).expanduser().resolve()
    print('[bootstrap] Temporary-tools cross-build starting', flush=True)
    print(f'[workspace] {project}', flush=True)
    manifest = json.loads(MANIFEST.read_text())
    if manifest.get('phase') != 'temp-tools' or not re.fullmatch(r'[a-zA-Z0-9_]+(?:-[a-zA-Z0-9_]+)+', manifest['target']):
        raise RuntimeError('invalid temp-tools manifest')
    print(f'[cross-toolchain] Locating verified archive: {cross_archive}', flush=True)
    cross_archive = resolve_cross_toolchain_archive(cross_archive)
    import_cross_toolchain(project, cross_archive, manifest['target'])
    done = set()
    total = len(manifest['packages'])
    for index, pkg in enumerate(manifest['packages'], 1):
        print(f'[{index}/{total}] {pkg["name"]}: constructing', flush=True)
        if pkg.get('state') != 'executable' or pkg.get('execution') != 'host-cross':
            raise RuntimeError('unsupported package entry: ' + pkg['name'])
        for dependency in pkg.get('requires', []):
            if dependency != 'cross-toolchain' and dependency not in done:
                raise RuntimeError('package dependency out of order: ' + pkg['name'] + ' -> ' + dependency)
        package_run(project, manifest, pkg, cross_archive)
        done.add(pkg['name'])
    print(f'[bootstrap] Temporary-tools cross-build complete: {len(done)} packages', flush=True)
    return 0

def native_recipe_argv(step, values, explicit_native=False):
    """Translate host-cross steps, or preserve explicit native recipe arguments."""
    argv = []
    for arg in step['argv']:
        if not explicit_native and (arg.startswith(('--build=', '--host=', '--target=')) or
                arg.startswith(('CC=', 'CXX=', 'CC_FOR_BUILD=', 'CXX_FOR_BUILD=',
                                'AR=', 'RANLIB=', 'LDFLAGS='))):
            continue
        if arg.startswith('/work/src/'):
            arg = '/work/native/{name}/src/' + arg[len('/work/src/'):]
        arg = arg.replace('/work/stage', '/work/native/{name}/stage')
        argv.append(arg.format_map(values))
    if not argv or any(not value for value in argv):
        raise RuntimeError('invalid native recipe argv')
    return argv


def native_build(project):
    """Host stages verified inputs; only Distro executes build steps.

    Native outputs are staged and hashed; cross-built installed tools are NOT
    overwritten. A separate ownership handoff is required for promotion.
    """
    if os.geteuid() == 0:
        raise RuntimeError('invoke native build as ordinary user')
    project = Path(project).expanduser().resolve()
    root, state = project / 'root', project / '.distro-bootstrap'
    if (root.is_symlink() or not root.is_dir() or
            not (state / 'temp-tools-import.json').is_file()):
        raise RuntimeError('native rebuild requires a verified temp-tools root')
    manifest = json.loads(MANIFEST.read_text())
    for binary in ('usr/bin/bash', 'usr/bin/env', 'usr/bin/make', 'usr/bin/gcc',
                   'usr/bin/gawk', 'usr/bin/grep', 'usr/bin/sed'):
        if not (root / binary).is_file():
            raise RuntimeError('native build prerequisite absent: ' + binary)
    if (root / 'work').is_symlink() or (root / 'work/native').is_symlink():
        raise RuntimeError('unsafe native work directory')
    for pkg in manifest['packages']:
        name = pkg['name']
        recipe_path = (HERE / pkg['recipe']).resolve()
        if not recipe_path.is_relative_to(HERE):
            raise RuntimeError('unsafe native recipe path')
        recipe = json.loads(recipe_path.read_text())
        if recipe.get('name') != name or recipe.get('execution') != 'host-cross':
            raise RuntimeError('unexpected native source recipe: ' + name)
        source_dir, archive = safe_source(pkg, state)
        signature = hashlib.sha256((common.sha(recipe_path) + ':' +
                                    json.dumps(pkg, sort_keys=True) + ':native-v1').encode()).hexdigest()
        stamps = state / 'native-stamps'
        stamps.mkdir(exist_ok=True)
        stamp = stamps / (name + '.json')
        if stamp.exists():
            record = json.loads(stamp.read_text())
            if record.get('signature') != signature or not record.get('artifacts'):
                raise RuntimeError('stale native stamp: ' + name)
            for rel, digest in record['artifacts'].items():
                if common.sha(root / rel) != digest:
                    raise RuntimeError('native staged artifact modified: ' + rel)
            print('[native] ' + name + ': SKIP verified stage', flush=True)
            continue
        # Target-root /work is a private directory belonging to the native build.
        work = root / 'work' / 'native' / name
        if work.exists():
            raise RuntimeError('existing uncommitted native workspace: ' + str(work))
        work.mkdir(parents=True)
        (work / 'build').mkdir()
        (work / 'stage').mkdir()
        shutil.copytree(source_dir, work / 'src', symlinks=True)
        for prereq in pkg.get('build_prerequisites', []):
            src = state / 'sources' / ('prerequisite-' + prereq['name'] + '-' + prereq['version']) / (prereq['name'] + '-' + prereq['version'])
            archive_path = state / 'sources' / prereq['url'].rsplit('/', 1)[-1]
            if (not src.is_dir() or not archive_path.is_file() or
                    hashlib.sha512(archive_path.read_bytes()).hexdigest() != prereq['sha512']):
                raise RuntimeError('verified GCC prerequisite cache unavailable: ' + prereq['name'])
            shutil.copytree(src, work / 'src' / prereq['name'], symlinks=True)
        values = {'name': name, 'target': manifest['target'], 'version': pkg['version'],
                  'jobs': str(min(os.cpu_count() or 2, 8))}
        lines = ['#!/usr/bin/bash', 'set -euo pipefail',
                 'cd ' + __import__('shlex').quote('/work/native/' + name + '/build')]
        explicit_native = 'native_steps' in recipe
        for step in recipe.get('native_steps', recipe['steps']):
            if not isinstance(step, dict) or set(step) != {'name', 'argv'}:
                raise RuntimeError('unsupported native recipe step: ' + name)
            lines.append('echo ' + __import__('shlex').quote('native step=' + step['name']))
            lines.append(' '.join(__import__('shlex').quote(a) for a in native_recipe_argv(step, values, explicit_native)))
        script = work / 'build.sh'
        script.write_text('\n'.join(lines) + '\n')
        script.chmod(0o700)
        # No host filesystems are bound. --clearenv plus env -i is intentional.
        guest = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
                 '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
                 '--bind', str(root), '/', '--proc', '/proc', '--dev', '/dev',
                 '--tmpfs', '/tmp', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                 '--setenv', 'LC_ALL', 'C', '--', '/usr/bin/env', '-i',
                 'HOME=/root', 'PATH=/usr/bin:/bin', 'LC_ALL=C',
                 '/usr/bin/bash', '/work/native/' + name + '/build.sh']
        print('[native] ' + name + ': isolated build', flush=True)
        common.run(guest, state / 'work' / ('native-' + name + '.log'), timeout=7200)
        stage_usr = work / 'stage' / 'usr'
        if not stage_usr.is_dir():
            raise RuntimeError('empty native stage: ' + name)
        artifacts = {}
        for path in sorted(stage_usr.rglob('*')):
            if path.is_file() and not path.is_symlink():
                artifacts[path.relative_to(root).as_posix()] = common.sha(path)
        if not artifacts:
            raise RuntimeError('no native staged files: ' + name)
        common.save(stamp, {'schema_version': 1, 'package': name,
                            'signature': signature, 'source_sha256': common.sha(archive),
                            'artifacts': artifacts})
        print(f'[native] {name}: COMPLETE; {len(artifacts)} staged files verified', flush=True)

if __name__ == '__main__':
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--native':
            sys.exit(native_build(sys.argv[2]) or 0)
        parser = argparse.ArgumentParser()
        parser.add_argument('project')
        parser.add_argument('cross_archive')
        args = parser.parse_args()
        sys.exit(main(args.project, args.cross_archive))
    except (OSError, RuntimeError, ValueError, KeyError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        print('[temp-tools] package-runner ERROR:', exc, file=sys.stderr, flush=True)
        sys.exit(1)
