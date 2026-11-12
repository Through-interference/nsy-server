"""Transient decoder failures and malformed feedback must not stop reception."""
import unittest
from unittest.mock import Mock
from nsy_server.rtp_receiver import DecoderRecovery
from nsy_server.link_core import normalize_json


class RecoveryTests(unittest.TestCase):
    def test_software_and_jpeg_failures_restart_with_backoff(self):
        now=[10.]
        recovery=DecoderRecovery('auto',clock=lambda:now[0])
        pipeline=Mock()
        for kind in ('software','jpeg'):
            self.assertTrue(recovery.handle(kind,'bad packet',pipeline,'NULL'))
            self.assertEqual(recovery.retry_at,now[0]+.5)
            now[0]+=1
        self.assertEqual(recovery.restarts,2)
        self.assertFalse(recovery.disabled)
        self.assertEqual(pipeline.set_state.call_count,2)

    def test_explicit_software_mode_recovers_runtime_failure(self):
        self.assertTrue(DecoderRecovery('software').handle('software','failed',Mock(),'NULL'))

    def test_missing_software_pipeline_fails_fast(self):
        self.assertFalse(DecoderRecovery('auto').handle('software','missing element',None,'NULL'))

    def test_forced_hardware_mode_remains_fail_fast(self):
        self.assertFalse(DecoderRecovery('hardware').handle('hardware','failed',Mock(),'NULL'))

    def test_malformed_stats_are_rejected_as_parse_errors(self):
        invalid=[[],None,{'type':'rx','packets':[]},
                 {'type':'rx','packets':{},'rx_ant_stats':[None]},
                 {'type':'rx','packets':{'out':[]}},
                 {'type':'rx','packets':{'out':[1]}},
                 {'type':'rx','packets':{'out':float('inf')}},
                 {'type':'rx','packets':{'out':True}}]
        for message in invalid:
            with self.subTest(message=message),self.assertRaises(ValueError):
                normalize_json(message)
        metric=normalize_json({'type':'rx','packets':{'out':[5,100]},'rx_ant_stats':[]})
        self.assertTrue(metric.active)


if __name__=='__main__':unittest.main()
