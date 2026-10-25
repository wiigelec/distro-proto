import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('native_plan', (Path(__file__).resolve().parents[1] / 'src/bootstrap/temporary_tools/native_plan.py'))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NativePlanTests(unittest.TestCase):
    def test_ordinary_cross_step_becomes_native(self):
        recipe = {'name':'bash','execution':'host-cross','steps':[
            {'name':'configure','argv':['/work/src/configure','--host={target}',
               '--prefix=/usr','CC={cc}','LDFLAGS={ldflags}']},
            {'name':'install','argv':['make','install','DESTDIR=/work/stage']}]}
        script = module.render_script({'name':'bash'}, recipe, 'x86_64-distro-linux-gnu', 2)
        self.assertIn('${DISTRO_WORK}/src/configure', script)
        self.assertIn('DESTDIR=${DISTRO_STAGE}', script)
        self.assertNotIn('--host=', script)
        self.assertNotIn('/toolchain', script)

    def test_native_recipe_is_retained(self):
        recipe={'name':'gcc','execution':'host-cross','native_steps':[
          {'name':'configure','argv':['/work/src/configure','CC=/usr/bin/gcc','--target={target}']}],
          'steps':[{'name':'irrelevant','argv':['/toolchain/unsafe']}]}
        script=module.render_script({'name':'gcc'},recipe,'x86_64-distro-linux-gnu',4)
        self.assertIn('CC=/usr/bin/gcc',script)
        self.assertNotIn('/toolchain',script)

    def test_cross_binary_forbidden(self):
        recipe={'name':'bash','execution':'host-cross','steps':[
          {'name':'configure','argv':['/toolchain/usr/bin/gcc']}]}
        with self.assertRaisesRegex(ValueError,'host-cross'):
            module.render_script({'name':'bash'},recipe,'x86_64-distro-linux-gnu',2)

    def test_missing_source_or_script_fails_closed(self):
        recipe={'name':'bash','execution':'host-cross','steps':[
          {'name':'configure','argv':['/work/src/configure','--foo={unknown}']}]}
        with self.assertRaisesRegex(ValueError,'unresolved'):
            module.render_script({'name':'bash'},recipe,'x86_64-distro-linux-gnu',2)

if __name__=='__main__':
    unittest.main()
