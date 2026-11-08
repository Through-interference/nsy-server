import ast,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from nsy_server.paths import PROJECT_ROOT,DEFAULT_CONFIG,module_command,child_env

class StandaloneTests(unittest.TestCase):
 def test_no_import_from_other_application_or_old_tree(self):
  for path in (PROJECT_ROOT/'src/nsy_server').glob('*.py'):
   for node in ast.walk(ast.parse(path.read_text())):
    names=([node.module] if isinstance(node,ast.ImportFrom) and node.module else [n.name for n in node.names] if isinstance(node,ast.Import) else [])
    for name in names:
     self.assertFalse(name.startswith(('bvs_camera','skvoz_video')),str(path)+': '+name)
 def test_help_from_another_working_directory(self):
  with tempfile.TemporaryDirectory() as cwd:
   for name in ('ground_station', 'rtp_receiver', 'radio_launcher', 'feedback', 'fdma', 'record_run', 'udp_link'):
    with self.subTest(module=name):
     p=subprocess.run(module_command(name,'--help'),cwd=cwd,env=child_env(),capture_output=True,text=True,timeout=10)
     self.assertEqual(p.returncode,0,p.stderr)
 def test_config_packaged_in_source(self):
  self.assertTrue(DEFAULT_CONFIG.is_file())
  self.assertEqual(json.loads(DEFAULT_CONFIG.read_text())['drones'][0]['link_id'],1)
