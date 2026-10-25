"""Prepare source-bound native build inputs in a fresh workspace."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from . import native_plan
from .package_runner import safe_source


def prepare_inputs(project, destination, jobs=2):
    project, destination = Path(project).expanduser().resolve(), Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('native input destination must be fresh')
    if destination.parent.is_symlink():
        raise RuntimeError('linked native input parent')
    state = project / '.distro-bootstrap'
    if state.is_symlink() or not state.is_dir():
        raise RuntimeError('missing verified source cache workspace')
    manifest_path = Path(__file__).with_name('temporary-tools-manifest.json')
    manifest = json.loads(manifest_path.read_text())
    destination.mkdir(mode=0o700)
    scripts, sources = destination / 'scripts', destination / 'sources'
    sources.mkdir()
    native_plan.prepare(manifest_path, manifest_path.parent, scripts, jobs)
    source_digests = {}
    for pkg in manifest['packages']:
        name = pkg['name']
        tree, archive = safe_source(pkg, state)
        # safe_source verifies the source archive; extracted cache reuse is
        # additionally bound to the same identity by its versioned location.
        source_digests[name] = hashlib.sha256(archive.read_bytes()).hexdigest()
        target = sources / name
        shutil.copytree(tree, target, symlinks=True)
        for item in pkg.get('build_prerequisites', []):
            n, version = item['name'], item['version']
            archived = state / 'sources' / item['url'].rsplit('/', 1)[-1]
            upstream = state / 'sources' / f'prerequisite-{n}-{version}' / f'{n}-{version}'
            if not archived.is_file() or archived.is_symlink() or not upstream.is_dir():
                raise RuntimeError('missing cached native compiler prerequisite: ' + n)
            if hashlib.sha512(archived.read_bytes()).hexdigest() != item['sha512']:
                raise RuntimeError('native compiler prerequisite checksum mismatch: ' + n)
            if (target / n).exists() or (target / n).is_symlink():
                raise RuntimeError('compiler prerequisite collides with upstream source: ' + n)
            shutil.copytree(upstream, target / n, symlinks=True)
    (destination / 'source-archive-hashes.json').write_text(json.dumps(source_digests, indent=2, sort_keys=True) + '\n')
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    print(prepare_inputs(args.project, args.output, args.jobs))


if __name__ == '__main__':
    main()
