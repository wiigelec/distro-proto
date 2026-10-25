"""FS-001 M2 source identity must come from the frozen M1 baseline."""
import hashlib
import json
import unittest
from bootstrap.temporary_tools.source_binding import (
    SourceBindingError, require_source_binding,
)


def canonical(obj):
    return hashlib.sha256(json.dumps(
        obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    ).encode()).hexdigest()


def fixtures():
    manifest = {'phase': 'temp-tools', 'packages': [
        {'name': 'bash', 'version': '5.3',
         'source': {'url': 'https://example.org/bash-5.3.tar.gz', 'sha256': 'a' * 64}}
    ]}
    baseline = {'status': 'qualified-mapping-baseline',
                'inventory_sha256': 'b' * 64,
                'packages': [{'distro_name': 'bash',
                              'upstream': {'version': '5.3', 'artifact': {
                                  'url': 'https://example.org/bash-5.3.tar.gz'}}}]}
    baseline['baseline_sha256'] = canonical(baseline)
    return manifest, baseline


class SourceBindingTests(unittest.TestCase):
    def test_matching_frozen_source(self):
        manifest, baseline = fixtures()
        self.assertIn('bash', require_source_binding(manifest, baseline))

    def test_unqualified_source_rejected(self):
        manifest, baseline = fixtures()
        manifest['packages'][0]['version'] = '5.4'
        with self.assertRaisesRegex(SourceBindingError, 'differs'):
            require_source_binding(manifest, baseline)

    def test_missing_mapping_rejected(self):
        manifest, baseline = fixtures()
        baseline['packages'] = []
        baseline.pop('baseline_sha256')
        baseline['baseline_sha256'] = canonical(baseline)
        with self.assertRaisesRegex(SourceBindingError, 'absent'):
            require_source_binding(manifest, baseline)

    def test_tampered_baseline_rejected(self):
        manifest, baseline = fixtures()
        baseline['packages'][0]['upstream']['version'] = '5.4'
        with self.assertRaisesRegex(SourceBindingError, 'digest'):
            require_source_binding(manifest, baseline)


if __name__ == '__main__':
    unittest.main()
