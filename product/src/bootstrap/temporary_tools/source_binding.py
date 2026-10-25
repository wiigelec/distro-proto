"""Fail-closed source binding between M2 recipes and the qualified M1 baseline.

The temporary manifest is currently an implementation recipe inventory, not a
source-version authority. No M2 package may use an independently selected
upstream release. This does not certify the completeness of the full FS-001
dependency closure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class SourceBindingError(ValueError):
    pass


def _canonical(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    ).encode('utf-8')).hexdigest()


def require_source_binding(manifest: dict, baseline: dict) -> dict:
    if not isinstance(baseline, dict):
        raise SourceBindingError('missing qualified execution baseline')
    if baseline.get('status') != 'qualified-mapping-baseline':
        raise SourceBindingError('M1 baseline was not qualified')
    recorded = baseline.get('baseline_sha256')
    document = dict(baseline)
    document.pop('baseline_sha256', None)
    if recorded != _canonical(document):
        raise SourceBindingError('M1 baseline digest mismatch')
    if baseline.get('inventory_sha256') is None:
        raise SourceBindingError('M1 baseline inventory identity missing')
    if not isinstance(manifest, dict) or manifest.get('phase') != 'temp-tools':
        raise SourceBindingError('invalid M2 manifest')
    selected = baseline.get('packages')
    if not isinstance(selected, list):
        raise SourceBindingError('M1 source mappings missing')
    by_name = {item.get('distro_name'): item for item in selected if isinstance(item, dict)}
    if len(by_name) != len(selected):
        raise SourceBindingError('duplicate or malformed M1 source mapping')
    bindings = {}
    for pkg in manifest.get('packages', []):
        name = pkg.get('name')
        owner = pkg.get('upstream_name', name)
        reference = by_name.get(owner)
        if reference is None:
            raise SourceBindingError(f'{name}: absent from qualified full-manifest mapping')
        upstream = reference.get('upstream', {})
        artifact = upstream.get('artifact', {})
        source = pkg.get('source', {})
        if (upstream.get('version') != pkg.get('version') or
                artifact.get('url') != source.get('url')):
            raise SourceBindingError(f'{name}: source differs from qualified baseline')
        # Source identity is established by the qualified upstream mapping.
        # A recipe checksum is an additional artifact verification, not a
        # substitute for source-identity correspondence.
        if 'sha256' not in source and 'sha512' not in source:
            raise SourceBindingError(f'{name}: verified source digest required')
        bindings[name] = {
            'source_owner': owner,
            'upstream_version': upstream['version'],
            'source_url': artifact['url'],
            'baseline_sha256': recorded,
        }
    if len(bindings) != len(manifest.get('packages', [])):
        raise SourceBindingError('duplicate M2 package name')
    return bindings


def baseline_for_cross_archive(archive: Path) -> dict:
    """Find the immutable execution-local baseline beside the M1 handoff."""
    archive = Path(archive).resolve(strict=True)
    if archive.name != 'cross-toolchain-root.tar.xz':
        raise SourceBindingError('invalid M1 archive name')
    baseline_path = archive.parent.parent / '.distro-bootstrap' / 'baseline.json'
    if not baseline_path.is_file() or baseline_path.is_symlink():
        raise SourceBindingError('qualified M1 baseline missing beside cross archive')
    return json.loads(baseline_path.read_text(encoding='utf-8'))
