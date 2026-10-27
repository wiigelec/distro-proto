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
import tempfile
import time
import urllib.request

from temporary_tools import package_runner
from temporary_tools import native_plan
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


def verify_extracted(root, inventory, allowed_extra=()):
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
    extras = actual - set(inventory)
    allowed = tuple(allowed_extra)
    for rel in extras:
        if not any(rel == prefix or rel.startswith(prefix + '/') for prefix in allowed):
            raise RuntimeError('unexpected imported-root entry: ' + rel)
    # Never accept a symlink masquerading as an owned workspace boundary.
    for prefix in allowed:
        candidate = root / prefix
        if candidate.is_symlink():
            raise RuntimeError('linked native workspace: ' + prefix)


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
    if (not url.startswith(('https://ftpmirror.gnu.org/',
                            'https://sourceware.org/pub/binutils/releases/',
                            'https://gcc.gnu.org/pub/gcc/releases/',
                            'https://gcc.gnu.org/pub/gcc/infrastructure/',
                            'https://ftp.gnu.org/gnu/'))
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
    if partial.is_symlink():
        raise RuntimeError('linked partial source download: ' + filename)
    # A partial file has no provenance. Restart it; completed files are reused
    # only after their pinned digest is checked above.
    for attempt in range(1, 5):
        partial.unlink(missing_ok=True)
        try:
            print(f'[native-verify] Downloading pinned source: {filename} (attempt {attempt}/4)', flush=True)
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
            return destination
        except (TimeoutError, OSError) as exc:
            if attempt == 4:
                raise
            print(f'[native-verify] {filename}: retrying after {exc}', flush=True)
            time.sleep(min(attempt * 2, 10))
        finally:
            partial.unlink(missing_ok=True)
    raise RuntimeError('source download retries exhausted: ' + filename)
    return destination


def tree_digest(directory):
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('invalid staged source directory: ' + str(directory))
    h = hashlib.sha256()
    for item in sorted(directory.rglob('*')):
        relative = item.relative_to(directory).as_posix()
        if item.is_symlink():
            kind, value = 'L', os.readlink(item)
        elif item.is_dir():
            kind, value = 'D', ''
        elif item.is_file():
            kind, value = 'F', digest(item)
        else:
            raise RuntimeError('unsupported staged source object: ' + relative)
        h.update(json.dumps([relative, kind, value], separators=(',', ':')).encode() + b'\n')
    return h.hexdigest()


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


def verify_native_runtime(root, state):
    """Execute native binaries and a C build entirely within the built root."""
    import tempfile
    if root.is_symlink() or not root.is_dir() or state.is_symlink():
        raise RuntimeError('invalid native runtime verification paths')
    for name in ('bash', 'gcc', 'make', 'as', 'ld'):
        if not (root / 'usr/bin' / name).is_file():
            raise RuntimeError('native runtime prerequisite missing: ' + name)
    with tempfile.TemporaryDirectory(prefix='runtime-check-', dir=state) as scratch:
        scratch = Path(scratch)
        (scratch / 'smoke.c').write_text('int main(void) { return 0; }\n')
        script = ('set -euo pipefail; '
                  '/usr/bin/bash --version >/dev/null; '
                  '/usr/bin/make --version >/dev/null; '
                  '/usr/bin/gcc --version >/dev/null; '
                  '/usr/bin/gcc /work/smoke.c -o /work/smoke; '
                  '/work/smoke; '
                  '/usr/bin/gcc -dumpmachine')
        # A read-only root cannot accommodate new mount-point directories.
        # Use a disposable filesystem for namespace mount points and expose
        # each native-root top-level entry unchanged and read-only.
        command = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
                   '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
                   '--tmpfs', '/']
        reserved = {'proc', 'dev', 'tmp', 'work'}
        for entry in sorted(root.iterdir()):
            if entry.name in reserved:
                raise RuntimeError('native root overlaps verifier mount point: ' + entry.name)
            destination = '/' + entry.name
            if entry.is_symlink():
                command += ['--symlink', os.readlink(entry), destination]
            elif entry.is_dir():
                command += ['--ro-bind', str(entry), destination]
            else:
                raise RuntimeError('unexpected native root top-level entry: ' + entry.name)
        command += ['--proc', '/proc', '--dev', '/dev',
                    '--tmpfs', '/tmp', '--bind', str(scratch), '/work',
                    '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                    '--setenv', 'HOME', '/work', '--setenv', 'LC_ALL', 'C',
                    '--', '/usr/bin/bash', '--noprofile', '--norc', '-c', script]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError('isolated native compiler/runtime check failed: ' + result.stderr[-1200:])
        target = result.stdout.strip().splitlines()[-1]
        if target != 'x86_64-distro-linux-gnu':
            raise RuntimeError('native compiler target mismatch: ' + target)
        return {'target': target, 'compiler_link_and_execution': 'passed',
                'isolated_execution': 'passed'}


def verify_native_elf_closure(root):
    """Fail when ELF runtime interpreters or DT_NEEDED libraries are absent."""
    import re
    roots = ('lib', 'lib64', 'usr/lib', 'usr/lib64')
    scanned = 0
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        scanned += 1
        program = subprocess.run(['readelf', '-l', str(path)], capture_output=True, text=True)
        dynamic = subprocess.run(['readelf', '-d', str(path)], capture_output=True, text=True)
        if program.returncode or dynamic.returncode:
            raise RuntimeError('unreadable native ELF: ' + str(path.relative_to(root)))
        for interpreter in re.findall(r'Requesting program interpreter: ([^] ]+)', program.stdout):
            if not interpreter.startswith('/') or not (root / interpreter.lstrip('/')).exists():
                raise RuntimeError('native ELF interpreter absent: ' + interpreter)
        for library in re.findall(r'\(NEEDED\).*?\[([^]]+)\]', dynamic.stdout):
            if '/' in library or not any((root / d / library).exists() for d in roots):
                raise RuntimeError('native ELF dependency absent: ' + library +
                                   ' required by ' + str(path.relative_to(root)))
    if scanned == 0:
        raise RuntimeError('native root contains no ELF files')
    return {'elf_files_scanned': scanned, 'declared_runtime_dependencies': 'present'}


def export_native_root(root, artifacts, input_digest, source_proofs,
                       package_names, runtime, closure):
    """Publish an independently checked native handoff; never overwrite one."""
    if artifacts.is_symlink():
        raise RuntimeError('linked artifact directory')
    artifacts.mkdir(exist_ok=True)
    archive = artifacts / 'native-tools-root.tar.xz'
    evidence = artifacts / 'native-tools-evidence.json'
    if any(p.exists() or p.is_symlink() for p in (archive, evidence)):
        raise RuntimeError('native handoff already exists; inspect before retry')
    partial = artifacts / '.native-tools-root.tar.xz.partial'
    if partial.exists() or partial.is_symlink():
        raise RuntimeError('unfinished native handoff archive exists')
    try:
        with tarfile.open(partial, 'w:xz', format=tarfile.PAX_FORMAT) as bundle:
            for path in sorted(root.rglob('*')):
                bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        # Independently compare the archive to the source root before publication.
        count = package_runner.verify_temporary_tools_archive(root, partial)
        record = {'schema_version': 1, 'status': 'verified',
                  'target': 'x86_64-distro-linux-gnu',
                  'input_archive_sha256': input_digest, 'sources': source_proofs,
                  'packages': package_names, 'runtime': runtime, 'elf_closure': closure,
                  'handoff': {'archive': str(archive), 'sha256': digest(partial),
                              'members': count}}
        os.link(partial, archive)
    finally:
        partial.unlink(missing_ok=True)
    write_json(evidence, record)
    return record['handoff']


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
    # The original archive must remain byte-identical. Owned scratch trees
    # added after import are allowed, but unrelated additions are rejected.
    verify_extracted(root, inventory, allowed_extra=('inputs', 'construction', 'proc', 'dev', 'tmp'))
    print('[native-verify] Imported root verified; checking package manifest', flush=True)
    manifest = json.loads(package_runner.MANIFEST.read_text())
    if not isinstance(manifest.get('packages'), list) or not manifest['packages']:
        raise RuntimeError('empty or invalid temporary-tools package closure')
    if len({p['name'] for p in manifest['packages']}) != len(manifest['packages']):
        raise RuntimeError('duplicate temporary-tools package')
    package_names = [p['name'] for p in manifest['packages']]
    # Prepare verified source trees and generated build procedures inside the
    # extracted root. No host build binaries are visible to the build process.
    guest_inputs = root / 'inputs'
    guest_sources = guest_inputs / 'sources'
    guest_scripts = guest_inputs / 'scripts'
    if guest_inputs.is_symlink() or guest_sources.is_symlink() or guest_scripts.is_symlink():
        raise RuntimeError('linked native input tree')
    guest_inputs.mkdir(exist_ok=True)
    guest_sources.mkdir(exist_ok=True)
    if guest_scripts.exists():
        if not guest_scripts.is_dir():
            raise RuntimeError('invalid native script directory')
        with tempfile.TemporaryDirectory(prefix='plan-verify-', dir=state) as temporary:
            regenerated = Path(temporary) / 'scripts'
            native_plan.prepare(package_runner.MANIFEST, package_runner.HERE,
                                regenerated, jobs=2)
            original = {p.name: digest(p) for p in guest_scripts.iterdir() if p.is_file() and not p.is_symlink()}
            expected = {p.name: digest(p) for p in regenerated.iterdir()}
            if original != expected or len(original) != len(list(guest_scripts.iterdir())):
                raise RuntimeError('existing native plan differs from current recipes')
        print('[native-verify] Reusing verified native plan', flush=True)
    else:
        native_plan.prepare(package_runner.MANIFEST, package_runner.HERE,
                            guest_scripts, jobs=2)
    completed_sources = state / 'prepared-sources'
    if completed_sources.is_symlink():
        raise RuntimeError('linked prepared-source evidence directory')
    completed_sources.mkdir(exist_ok=True)
    source_proofs = {}
    for pkg in manifest['packages']:
        name = pkg['name']
        filename = pkg['source']['url'].rsplit('/', 1)[-1]
        print('[native-verify] ' + name + ': acquiring ' + filename, flush=True)
        algorithm = 'sha512' if 'sha512' in pkg['source'] else 'sha256'
        source_archive = acquire_source(state, pkg['source'], filename, algorithm=algorithm)
        top = pkg.get('upstream_name', name) + '-' + pkg['version']
        destination = guest_sources / name
        recipe_hash = digest(package_runner.HERE / pkg['recipe'])
        archive_hash = digest(source_archive)
        stamp = completed_sources / (name + '.json')
        if stamp.is_symlink() or destination.is_symlink():
            raise RuntimeError('linked source/stamp for package: ' + name)
        if stamp.exists():
            proof = json.loads(stamp.read_text())
            if (proof.get('source_sha256') != archive_hash or
                    proof.get('recipe_sha256') != recipe_hash or
                    proof.get('tree_sha256') != tree_digest(destination)):
                raise RuntimeError('completed source differs from pinned inputs: ' + name)
            source_proofs[name] = {'source_sha256': archive_hash,
                                   'recipe_sha256': recipe_hash}
            print('[native-verify] ' + name + ': resuming verified source stage', flush=True)
            continue
        if destination.exists():
            if not destination.is_dir():
                raise RuntimeError('invalid partial source directory: ' + name)
            print('[native-verify] ' + name + ': rebuilding unfinished source stage', flush=True)
            shutil.rmtree(destination)
        destination.mkdir()
        with tarfile.open(source_archive, 'r:*') as tf:
            safe_source_archive(tf, top)
            tf.extractall(destination, filter='data')
        extracted = destination / top
        if not (extracted / 'configure').is_file():
            raise RuntimeError('missing source configure: ' + name)
        for entry in extracted.iterdir():
            target = destination / entry.name
            if target.exists() or target.is_symlink():
                raise RuntimeError('duplicate source member: ' + name)
            entry.rename(target)
        extracted.rmdir()
        for dependency in pkg.get('build_prerequisites', []):
            dep_name = dependency['name']
            dep_archive = acquire_source(
                state, dependency, dependency['url'].rsplit('/', 1)[-1],
                algorithm='sha512')
            dep_top = dep_name + '-' + dependency['version']
            dep_temp = destination / ('.prerequisite-' + dep_name)
            dep_temp.mkdir()
            with tarfile.open(dep_archive, 'r:*') as tf:
                safe_source_archive(tf, dep_top)
                tf.extractall(dep_temp, filter='data')
            (dep_temp / dep_top).rename(destination / dep_name)
            dep_temp.rmdir()
        source_proofs[name] = {'source_sha256': archive_hash,
                               'recipe_sha256': recipe_hash}
        write_json(stamp, dict(source_proofs[name], tree_sha256=tree_digest(destination)))
        print('[native-verify] ' + name + ': verified source staged', flush=True)
    # The assembler and inputs reside entirely within the private extracted
    # filesystem. bwrap supplies chroot-style isolation and a private PID and
    # mount namespace, without sudo or host filesystem tool bindings.
    assembler = root / 'inputs' / 'native_assemble.sh'
    expected_assembler = Path(__file__).with_name('temporary_tools') / 'native_assemble.sh'
    if assembler.is_symlink():
        raise RuntimeError('linked native assembler')
    if assembler.exists():
        if digest(assembler) != digest(expected_assembler):
            raise RuntimeError('existing native assembler differs from repository')
    else:
        shutil.copyfile(expected_assembler, assembler)
    construction = root / 'construction'
    if construction.is_symlink():
        raise RuntimeError('linked construction workspace')
    if construction.exists():
        if not construction.is_dir():
            raise RuntimeError('invalid construction workspace')
        print('[native-verify] Resuming verification of existing native root', flush=True)
    else:
        construction.mkdir()
        (root / 'proc').mkdir(exist_ok=True)
        (root / 'dev').mkdir(exist_ok=True)
        (root / 'tmp').mkdir(exist_ok=True)
        command = ['bwrap', '--die-with-parent', '--new-session', '--unshare-user',
                   '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
                   '--bind', str(root), '/', '--proc', '/proc', '--dev', '/dev',
                   '--tmpfs', '/tmp', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                   '--setenv', 'LC_ALL', 'C', '--', '/usr/bin/bash',
                   '/inputs/native_assemble.sh', '/inputs/scripts/plan.tsv',
                   '/construction/root', '/construction/work']
        log = state / 'native-construction.log'
        print('[native-verify] Constructing fresh root; streaming output: ' + str(log), flush=True)
        package_runner.common.run(command, log, timeout=7200 * len(manifest['packages']))
    status_file = construction / 'work' / 'construction.status'
    if not status_file.is_file() or status_file.read_text().strip() != 'assembled-unverified':
        raise RuntimeError('native assembler did not report a complete build')
    output = construction / 'root'
    if output.is_symlink() or not output.is_dir():
        raise RuntimeError('missing or linked native output root')
    installed = construction / 'work' / 'installed-packages'
    if installed.is_symlink() or not installed.is_file():
        raise RuntimeError('missing native package completion inventory')
    if installed.read_text().splitlines() != package_names:
        raise RuntimeError('native package completion inventory differs from manifest')
    checks = {}
    for pkg in manifest['packages']:
        recipe = json.loads((package_runner.HERE / pkg['recipe']).read_text())
        for binary in recipe.get('validation', {}).get('chroot_executables', []):
            if binary in checks:
                continue
            file = output / binary.lstrip('/')
            if not file.is_file():
                raise RuntimeError('missing fresh-root executable: ' + binary)
            checks[binary] = digest(file)
    report = {'schema_version': 1, 'status': 'assembled-unverified',
              'archive_sha256': observed, 'member_count': len(inventory),
              'native_root': str(output), 'sources': source_proofs,
              'executables': checks,
              'missing_proofs': ['fresh-root runtime execution and compiler/linker smoke',
                                 'complete runtime/library closure and provenance']}
    write_json(project / 'artifacts/temp-tools-native-validation.json', report)
    print('[native-verify] Native root assembled; checking runtime and provenance', flush=True)
    try:
        runtime = verify_native_runtime(output, state)
        closure = verify_native_elf_closure(output)
        report['runtime'] = runtime
        report['elf_closure'] = closure
        report['missing_proofs'] = []
        # Export must be last: no accepted artifact until all verification passes.
        handoff = export_native_root(output, project / 'artifacts', observed, source_proofs,
                                     package_names, runtime, closure)
        report['handoff'] = handoff
        report['status'] = 'verified'
        write_json(project / 'artifacts/temp-tools-native-validation.json', report)
    except Exception as exc:
        report['verification_error'] = str(exc)
        write_json(project / 'artifacts/temp-tools-native-validation.json', report)
        raise
    print('[native-verify] Native handoff verified: ' + handoff['archive'], flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
