"""Prepare native package build scripts from a frozen recipe inventory.

Run this during input preparation. Generated scripts run with the native
assembler under the isolated build root; this module does not execute builds.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

NAME = re.compile(r'[a-z0-9][a-z0-9+._-]*\Z')
PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z_0-9]*)\}')
DISALLOWED = ('/toolchain', '--sysroot=', '-fno-link-libatomic')
DROP_PREFIXES = ('--host=', '--build=', '--target=', 'CC=', 'CXX=', 'CC_FOR_BUILD=',
                 'CXX_FOR_BUILD=', 'CC_FOR_TARGET=', 'CXX_FOR_TARGET=', 'AR=',
                 'RANLIB=', 'LDFLAGS=', 'LD=', 'AS=', 'NM=', 'STRIP=',
                 'LDFLAGS_FOR_TARGET=', 'target_configargs=')


def convert_arg(arg, *, explicit, target, jobs):
    if not isinstance(arg, str) or not arg or '\x00' in arg or '\n' in arg:
        raise ValueError('invalid build argument')
    if not explicit and arg.startswith(DROP_PREFIXES):
        return None
    def replace(m):
        mapping = {'target': target, 'version': '', 'jobs': str(jobs)}
        if m.group(1) not in mapping or m.group(1) == 'version':
            raise ValueError('unresolved build placeholder: ' + m.group(1))
        return mapping[m.group(1)]
    arg = PLACEHOLDER.sub(replace, arg)
    arg = arg.replace('/work/src/', '${DISTRO_WORK}/src/')
    arg = arg.replace('/work/stage', '${DISTRO_STAGE}')
    if any(x in arg for x in DISALLOWED) or re.search(r'(?<!\$)\{', arg):
        raise ValueError('host-cross argument survived native translation: ' + arg)
    return arg


def render_script(pkg, recipe, target, jobs):
    name = pkg['name']
    explicit = 'native_steps' in recipe
    steps = recipe['native_steps'] if explicit else recipe['steps']
    if recipe.get('name') != name or recipe.get('execution') != 'host-cross':
        raise ValueError('recipe identity or execution differs: ' + name)
    if not steps or not isinstance(steps, list):
        raise ValueError('empty recipe steps: ' + name)
    lines = ['#!/usr/bin/bash', 'set -euo pipefail', 'export PATH=/usr/bin:/bin LC_ALL=C',
             ': "${DISTRO_STAGE:?}" "${DISTRO_WORK:?}"',
             'mkdir -p -- "$DISTRO_WORK/build" "$DISTRO_WORK/src"',
             # The caller supplies a preverified, extracted source tree.
             'test -f "/inputs/sources/' + name + '/configure" || { echo "missing source: ' + name + '" >&2; exit 1; }',
             'cp -a -- "/inputs/sources/' + name + '/." "$DISTRO_WORK/src/"',
             'cd "$DISTRO_WORK/build"']
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'name', 'argv'} or not isinstance(step['argv'], list):
            raise ValueError('invalid recipe step: ' + name)
        converted = [convert_arg(a, explicit=explicit, target=target, jobs=jobs)
                     for a in step['argv']]
        converted = [a for a in converted if a is not None]
        if not converted:
            raise ValueError('empty translated recipe step: ' + name)
        if converted[0] == 'make':
            converted[0] = '/usr/bin/make'
        # All arguments are shell words, including words containing paths.
        # Expand only the two planner-owned variables, always under quotes;
        # unquoted variable expansion would split paths containing whitespace.
        def quote(value):
            markers = {'${DISTRO_WORK}': '__DISTRO_WORK_TOKEN__',
                       '${DISTRO_STAGE}': '__DISTRO_STAGE_TOKEN__'}
            for variable, marker in markers.items():
                value = value.replace(variable, marker)
            # Quote shell metacharacters before restoring trusted expansions.
            value = (value.replace('\\', '\\\\').replace('"', '\\"')
                          .replace('$', '\\$').replace('`', '\\`'))
            for variable, marker in markers.items():
                value = value.replace(marker, variable)
            return '"' + value + '"'
        lines.append(' '.join(quote(a) for a in converted))
    return '\n'.join(lines) + '\n'


def prepare(manifest_path, recipe_root, output, jobs):
    if output.exists() or output.is_symlink():
        raise ValueError('native build plan output must be fresh')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('phase') != 'temp-tools' or not isinstance(manifest.get('packages'), list):
        raise ValueError('invalid package inventory')
    target = manifest['target']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', target) or not 1 <= jobs <= 128:
        raise ValueError('invalid target or job count')
    prepared = []
    done = set()
    for pkg in manifest['packages']:
        name = pkg['name']
        if not NAME.fullmatch(name) or name in done:
            raise ValueError('invalid/duplicate package: ' + name)
        if pkg.get('state') != 'executable':
            raise ValueError('nonexecutable package: ' + name)
        if any(dep != 'cross-toolchain' and dep not in done for dep in pkg.get('requires', [])):
            raise ValueError('unresolved dependencies: ' + name)
        relative = Path(pkg['recipe'])
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.json':
            raise ValueError('unsafe recipe path')
        recipe_path = recipe_root / relative
        if recipe_path.is_symlink() or not recipe_path.is_file():
            raise ValueError('missing recipe')
        recipe = json.loads(recipe_path.read_text())
        script = render_script(pkg, recipe, target, jobs)
        prepared.append((name, script, hashlib.sha256(recipe_path.read_bytes()).hexdigest()))
        done.add(name)
    if not prepared:
        raise ValueError('empty package inventory')
    output.mkdir(mode=0o700, parents=True)
    for name, script, _ in prepared:
        (output / (name + '.sh')).write_text(script)
    (output / 'plan.tsv').write_text(''.join(
        f'{name}\t/inputs/scripts/{name}.sh\n' for name, _, _ in prepared))
    (output / 'source-recipe-hashes.json').write_text(json.dumps(
        {'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
         'recipes': {name: digest for name, _, digest in prepared}}, indent=2) + '\n')
    return len(prepared)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--recipe-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    print('Prepared', prepare(args.manifest, args.recipe_root, args.output, args.jobs), 'native procedures')

if __name__ == '__main__':
    main()
