import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'src/bootstrap/temporary_tools/native_assemble.sh'


class NativeAssemblyTests(unittest.TestCase):
    def run_case(self, entries, scripts):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, content in scripts.items():
                (base / name).write_text(content)
            plan = base / 'plan.tsv'
            plan.write_text(entries.replace('{base}', str(base)))
            output = base / 'result'
            work = base / 'work'
            process = subprocess.run(
                ['bash', str(SCRIPT), str(plan), str(output), str(work)],
                capture_output=True, text=True)
            status = (work / 'construction.status').read_text().strip() if (
                work / 'construction.status').exists() else None
            payload = (output / 'usr/share/example').read_text() if (
                output / 'usr/share/example').is_file() else None
            return process.returncode, status, payload

    def test_builds_and_assembles_without_claiming_acceptance(self):
        script = ('mkdir -p "$DISTRO_STAGE/usr/share"\n'
                  'printf ok > "$DISTRO_STAGE/usr/share/example"\n')
        code, status, payload = self.run_case(
            'example\t{base}/build.sh\n', {'build.sh': script})
        self.assertEqual((code, status, payload),
                         (0, 'assembled-unverified', 'ok'))

    def test_failing_build_stops_construction(self):
        code, status, _ = self.run_case(
            'example\t{base}/build.sh\n', {'build.sh': 'exit 3\n'})
        self.assertNotEqual(code, 0)
        self.assertEqual(status, 'incomplete')

    def test_collision_is_rejected(self):
        script = ('mkdir -p "$DISTRO_STAGE/usr/share"\n'
                  'printf ok > "$DISTRO_STAGE/usr/share/example"\n')
        code, status, _ = self.run_case(
            'one\t{base}/build.sh\ntwo\t{base}/build.sh\n',
            {'build.sh': script})
        self.assertNotEqual(code, 0)
        self.assertEqual(status, 'incomplete')

    def test_duplicate_package_is_rejected(self):
        code, _, _ = self.run_case(
            'one\t{base}/build.sh\none\t{base}/build.sh\n',
            {'build.sh': 'exit 0\n'})
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
