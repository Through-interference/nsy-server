"""Laptop NSU orchestration without changing physical interfaces."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nsy_server.paths import DEFAULT_CONFIG

class GroundStationTests(unittest.TestCase):
    def options(self,*args):
        from nsy_server.ground_station import parser
        return parser().parse_args(list(args))

    def test_udp_plan_runs_receiver_only(self):
        from nsy_server.ground_station import load_drone,build_commands
        args=self.options('--mode','udp');commands=build_commands(args,load_drone(args))
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0][0],'receiver')
        self.assertIn('nsy_server.rtp_receiver',commands[0][1])
        self.assertIn('udp',commands[0][1])

    def test_demo_is_explicit_and_does_not_use_radio_or_keys(self):
        from nsy_server.ground_station import load_drone,build_commands
        args=self.options('--demo','--demo-codec','jpeg','--fps','30')
        commands=build_commands(args,load_drone(args))
        self.assertEqual([name for name,_ in commands],['receiver','demo'])
        self.assertIn('rtpjpegpay',commands[1][1])
        self.assertNotIn('wfb_rx',str(commands))
        self.assertNotIn('gs.key',str(commands))

    def test_h264_demo_encodes_rtp(self):
        from nsy_server.ground_station import load_drone,build_commands
        args=self.options('--demo');commands=build_commands(args,load_drone(args))
        self.assertIn('x264enc',commands[1][1]);self.assertIn('rtph264pay',commands[1][1])

    def test_radio_plan_selects_ground_role_and_explicit_interface(self):
        from nsy_server.ground_station import load_drone,build_commands
        args=self.options('--interface','usb_radio','--keys','/tmp/matching-keys')
        command=build_commands(args,load_drone(args))[0][1]
        self.assertIn('nsy_server.radio_launcher',command)
        self.assertEqual(command[command.index('--role')+1],'ground')
        self.assertEqual(command[command.index('--interface')+1],'usb_radio')
        self.assertEqual(command[command.index('--keys')+1],str(Path('/tmp/matching-keys').resolve()))

    def test_unknown_drone_rejected(self):
        from nsy_server.ground_station import load_drone
        with self.assertRaises(ValueError):load_drone(self.options('--drone','missing'))

    def test_ports_override_without_mutating_config(self):
        from nsy_server.ground_station import load_drone
        before=DEFAULT_CONFIG.read_bytes()
        args=self.options('--mode','udp','--video-port','15600','--http-port','18099')
        drone=load_drone(args)
        self.assertEqual(drone['gs_video_port'],15600);self.assertEqual(drone['gs_status_port'],18099)
        self.assertEqual(DEFAULT_CONFIG.read_bytes(),before)

    def test_radio_does_not_silently_override_port_plan(self):
        from nsy_server.ground_station import load_drone
        with self.assertRaises(ValueError):load_drone(self.options('--video-port','15600'))

    def test_invalid_port_rejected(self):
        from nsy_server.ground_station import load_drone
        with self.assertRaises(ValueError):load_drone(self.options('--mode','udp','--video-port','1'))

    def test_udp_diagnostics_do_not_require_radio(self):
        from nsy_server.ground_station import load_drone,readiness
        args=self.options('--mode','udp')
        with patch('nsy_server.ground_station.runtime_diagnostics',return_value={'ready':True,'missing':[]}),patch('nsy_server.ground_station.available_radios',return_value=[]):
            report=readiness(args,load_drone(args))
        self.assertTrue(report['ready']);self.assertEqual(report['mode'],'udp')

    def test_radio_check_rejects_interface_that_launcher_would_reject(self):
        from nsy_server.ground_station import load_drone,readiness
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'gs.key').write_bytes(b'test-only-key')
            for name in ('wfb_rx','wfb_tx'):(root/name).write_text('#!/bin/sh\n');(root/name).chmod(0o755)
            args=self.options('--keys',tmp,'--binary-dir',tmp,'--interface','usb_radio')
            with patch('nsy_server.ground_station.runtime_diagnostics',return_value={'ready':True,'missing':[]}),patch('nsy_server.ground_station.available_radios',return_value=[{'interface':'usb_radio'}]),patch('nsy_server.ground_station.shutil.which',return_value='/usr/bin/tool'),patch('nsy_server.ground_station.inspect_interface',side_effect=ValueError('Unverified WFB driver')):
                report=readiness(args,load_drone(args))
            self.assertFalse(report['ready'])
            self.assertTrue(any('Unverified WFB driver' in issue for issue in report['issues']))

    def test_radio_diagnostics_report_missing_key_without_creating_one(self):
        from nsy_server.ground_station import load_drone,readiness
        with tempfile.TemporaryDirectory() as tmp:
            args=self.options('--keys',tmp,'--interface','usb_radio','--binary-dir',tmp)
            with patch('nsy_server.ground_station.runtime_diagnostics',return_value={'ready':True,'missing':[]}),patch('nsy_server.ground_station.available_radios',return_value=[]):
                report=readiness(args,load_drone(args))
            self.assertFalse(report['ready']);self.assertTrue(any('gs.key' in issue for issue in report['issues']))
            self.assertFalse((Path(tmp)/'gs.key').exists())


class ReceiverLinkTests(unittest.TestCase):
    def test_link_starts_empty_then_reports_fresh_and_stale(self):
        from nsy_server.rtp_receiver import ReceiverLink
        now=[0.];link=ReceiverLink(clock=lambda:now[0])
        self.assertIsNone(link.status()['link'])
        link.update(dict(rssi=-65,snr=18,pre_loss=.1,post_loss=0,active=True))
        self.assertEqual(link.status()['link_state'],'receiving')
        self.assertEqual(link.status()['link']['rssi'],-65)
        now[0]=4
        self.assertEqual(link.status()['link_state'],'stale')
    def test_invalid_link_rejected(self):
        from nsy_server.rtp_receiver import ReceiverLink
        with self.assertRaises(ValueError):ReceiverLink().update(dict(rssi=-60,snr=20,pre_loss=0,post_loss=0,active='false'))


class RadioInterfaceTests(unittest.TestCase):
    def test_builtin_wlan0_remains_protected(self):
        from nsy_server.radio_launcher import inspect_interface
        with patch('nsy_server.radio_launcher.is_usb_interface',return_value=False):
            with self.assertRaises(ValueError):inspect_interface('wlan0')
    def test_dedicated_usb_wlan0_is_usable_on_wsl(self):
        from nsy_server.radio_launcher import inspect_interface
        with patch('nsy_server.radio_launcher.is_usb_interface',return_value=True),patch('nsy_server.radio_launcher.Path.resolve',return_value=Path('/drivers/8812au')),patch('nsy_server.radio_launcher.subprocess.check_output',side_effect=['[]','Interface wlan0\n type managed\n']):
            self.assertEqual(inspect_interface('wlan0'),'managed')
    def test_usb_with_active_network_address_is_protected(self):
        from nsy_server.radio_launcher import inspect_interface
        with patch('nsy_server.radio_launcher.is_usb_interface',return_value=True),patch('nsy_server.radio_launcher.subprocess.check_output',return_value='[{"addr_info":[{"scope":"global"}]}]'):
            with self.assertRaises(ValueError):inspect_interface('wlan0')

if __name__=='__main__':unittest.main()
