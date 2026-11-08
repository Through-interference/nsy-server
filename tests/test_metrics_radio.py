import math,json,unittest
from pathlib import Path
from nsy_server.paths import DEFAULT_CONFIG
from nsy_server.link_core import Metric,RawStats,normalize_json
from nsy_server.fdma import validate

class TelemetryTests(unittest.TestCase):
 def test_cumulative_counters_not_treated_as_interval(self):
  m=normalize_json(dict(type='rx',id='video rx',packets=dict(out=[90,90000],lost=[10,10000],frags=[120,120000],frags_lost=[12,12000]),rx_ant_stats=[dict(pkt_recv=0,rssi_avg=-20,snr_avg=40),dict(pkt_recv=100,rssi_avg=-70,snr_avg=15)]),'video rx')
  self.assertAlmostEqual(m.post_loss,.1);self.assertAlmostEqual(m.pre_loss,.1);self.assertEqual(m.rssi,-70)
 def test_wrong_stream_ignored(self):
  self.assertIsNone(normalize_json(dict(type='rx',id='mavlink rx'),'video rx'))
 def test_raw_parser_and_loss(self):
  p=RawStats();p.feed('100\tRX_ANT\t2412:1:20\t0\t20:-80:-70:-60:10:20:30')
  m=p.feed('100\tPKT\t24:1000:0:1:23:20:2:1:0:19:950:24:4')
  self.assertEqual(m.rssi,-70);self.assertAlmostEqual(m.post_loss,.05);self.assertAlmostEqual(m.pre_loss,4/24)
 def test_unsupported_counter_version_fails(self):
  with self.assertRaises(ValueError):RawStats().feed('100\tPKT\t1:2:3')
class FDMATests(unittest.TestCase):
 def config(self):return json.loads(DEFAULT_CONFIG.read_text())
 def test_example(self):validate(self.config())
 def test_overlap(self):
  c=self.config();c['drones'][1]['frequency_mhz']=2422
  with self.assertRaises(ValueError):validate(c)
 def test_reused_interface(self):
  c=self.config();c['drones'][1]['gs_interface']=c['drones'][0]['gs_interface']
  with self.assertRaises(ValueError):validate(c)
 def test_duplicate_output_port(self):
  c=self.config();c['drones'][1]['gs_video_port']=c['drones'][0]['gs_video_port']
  with self.assertRaises(ValueError):validate(c)
class ReviewFixTests(unittest.TestCase):
 def test_non_boolean_activity_rejected(self):
  with self.assertRaises(ValueError):Metric(-50,30,0,0,'false').validate()
 def test_hardware_failure_recovers_only_once_in_auto_mode(self):
  from nsy_server.rtp_receiver import DecoderRecovery
  from unittest.mock import Mock
  pipeline=Mock();r=DecoderRecovery('auto')
  self.assertTrue(r.handle('hardware','push-buffer failed',pipeline,'NULL'))
  pipeline.set_state.assert_called_once_with('NULL')
  self.assertTrue(r.disabled)
  self.assertFalse(r.handle('hardware','second failure',pipeline,'NULL'))
  self.assertFalse(r.handle('software','decoder failed',pipeline,'NULL'))
 def test_forced_hardware_does_not_silently_switch(self):
  from nsy_server.rtp_receiver import DecoderRecovery
  self.assertFalse(DecoderRecovery('hardware').handle('hardware','failed',None,'NULL'))
 def test_startup_failure_without_pipeline_can_recover(self):
  from nsy_server.rtp_receiver import DecoderRecovery
  self.assertTrue(DecoderRecovery('auto').handle('hardware','startup error',None,'NULL'))
 def test_fdma_rejects_two_virtual_interfaces_on_one_phy(self):
  from nsy_server.radio_launcher import inspect_physical_interfaces
  from unittest.mock import patch
  import tempfile
  with tempfile.TemporaryDirectory() as root:
   root=Path(root);phy=root/'phy0';phy.mkdir()
   for name in ('radioA','radioB'):
    (root/name).mkdir();(root/name/'phy80211').symlink_to(phy)
   with patch('nsy_server.radio_launcher.inspect_interface'):
    with self.assertRaisesRegex(ValueError,'independent'):inspect_physical_interfaces(['radioA','radioB'],root)
 def test_fdma_accepts_distinct_physical_radios(self):
  from nsy_server.radio_launcher import inspect_physical_interfaces
  from unittest.mock import patch
  import tempfile
  with tempfile.TemporaryDirectory() as root:
   root=Path(root)
   for i,name in enumerate(('radioA','radioB')):
    phy=root/('phy'+str(i));phy.mkdir();(root/name).mkdir();(root/name/'phy80211').symlink_to(phy)
   with patch('nsy_server.radio_launcher.inspect_interface'):
    self.assertEqual(len(inspect_physical_interfaces(['radioA','radioB'],root)),2)
 def test_fdma_status_ports_are_unique(self):
  c=json.loads(DEFAULT_CONFIG.read_text());c['drones'][1]['gs_status_port']=c['drones'][0]['gs_status_port']
  with self.assertRaises(ValueError):validate(c)
 def test_ground_receiver_and_feedback_share_configured_status(self):
  from nsy_server.radio_launcher import ground_feedback_command,ground_receiver_command
  c=validate(json.loads(DEFAULT_CONFIG.read_text()))
  for d in c['drones']:
   rx=ground_receiver_command(d);feedback=ground_feedback_command(d)
   port=rx[rx.index('--http-port')+1]
   self.assertEqual(feedback[feedback.index('--receiver-status')+1],'http://127.0.0.1:'+port+'/status')
   self.assertEqual(rx[rx.index('--port')+1],str(d['gs_video_port']))
