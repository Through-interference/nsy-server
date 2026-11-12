"""Evidence must separate localhost capacity from real radio/range acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from nsy_server.telemetry import Radio, parser, validate
from nsy_server.telemetry_report import distance, evaluate, load_run


class EvidenceTests(unittest.TestCase):
    def runs(self, mode='udp-test'):
        runs = []
        for role, session, peer in [('air', 'a'*32, 'g'*32), ('ground', 'g'*32, 'a'*32)]:
            manifest = dict(mode=mode, role=role, session=session, frequencies_mhz=[2412,2462],
                            link_id=1, requested_tx_power_limit_dbm=10,
                            site=dict(latitude=0, longitude=0 if role=='air' else .04,
                                      antenna_gain_dbi=2, feed_loss_db=1, eirp_limit_dbm=12))
            events = [dict(event='link_up', peer_session=peer),
                      dict(event='link_down'),
                      dict(event='channel_verified', frequency_mhz=2462, reported_tx_power_dbm=10),
                      dict(event='channel_changed', from_mhz=2412, to_mhz=2462),
                      dict(event='link_restored', frequency_mhz=2462, peer_session=peer)]
            runs.append(dict(manifest=manifest, report=dict(errors=[]), events=events, path=role))
        air, ground = runs
        for seq in range(1,2476):
            air['events'].append(dict(event='payload_sent',sequence=seq,bytes=1000,submitted=True))
            ground['events'].append(dict(event='payload_received',sequence=seq,bytes=1000,
                sender_session='a'*32,monotonic_s=(seq-1)*60/2475,sha256='checked'))
        air['events'].append(dict(event='benchmark_finished',duration_s=60))
        ground['report']['reception']=dict(unique_packets=2475,payload_bytes=2475000,
            measurement_seconds=60,payload_goodput_bps=330000,throughput_passed=True,
            source='generated_load',benchmark_end_received=True,loss_fraction=0)
        return air,ground

    def test_localhost_never_proves_radio_or_range(self):
        result=evaluate(*self.runs())
        self.assertEqual(result['throughput_status'],'localhost_pass_only')
        self.assertEqual(result['frequency_recovery_status'],'not_proven')
        self.assertGreater(result['supplied_coordinate_distance_m'],3000)
        self.assertFalse(result['all_requirements_confirmed'])
        self.assertEqual(result['air_power']['status'],'not_verified')
        self.assertIn('requires_field',result['range_status'])

    def test_radio_recovery_needs_both_readbacks_and_peer_sessions(self):
        air,ground=self.runs('radio')
        result=evaluate(air,ground)
        self.assertEqual(result['throughput_status'],'measured_on_radio')
        self.assertEqual(result['restored_backup_frequencies_mhz'],[2462])
        self.assertEqual(result['air_power']['status'],'within_operator_declared_limit')
        ground['events']=[e for e in ground['events'] if e['event']!='channel_verified']
        self.assertEqual(evaluate(air,ground)['frequency_recovery_status'],'not_proven')

    def test_duplicate_journal_records_cannot_inflate_throughput(self):
        air,ground=self.runs()
        ground['events'].append(copy.copy(ground['events'][-1]))
        with self.assertRaises(ValueError):evaluate(air,ground)

    def test_transmitter_and_receiver_payloads_must_match(self):
        air,ground=self.runs()
        air['events']=[e for e in air['events'] if not (e['event']=='payload_sent' and e['sequence']==1)]
        with self.assertRaises(ValueError):evaluate(air,ground)

    def test_evidence_files_are_verified_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manifest=dict(mode='udp-test',role='ground',session='a',frequencies_mhz=[2412,2462],link_id=1)
            (root/'manifest.json').write_text(json.dumps(manifest))
            (root/'events.jsonl').write_text('{}\n')
            report=dict(**manifest,evidence_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()})
            (root/'report.json').write_text(json.dumps(report))
            load_run(root)
            (root/'events.jsonl').write_text('tampered\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):load_run(root)

    def test_distance_is_a_calculation_not_confirmation(self):
        self.assertAlmostEqual(distance(dict(latitude=0,longitude=0),dict(latitude=0,longitude=.03)),3335.85,places=1)
        self.assertIsNone(distance(None,None))


class RadioReadbackTests(unittest.TestCase):
    def radio(self, text):
        args=validate(parser().parse_args(['--role','air','--interface','radio1','--tx-power-dbm','10']))
        radio=Radio(args,Mock())
        radio.run=Mock(side_effect=['',text])
        return radio

    def test_tuning_checks_driver_frequency_and_power(self):
        radio=self.radio('channel 11 (2462 MHz), width: 20 MHz\ntxpower 10.00 dBm\n')
        radio.tune(2462)
        radio.journal.write.assert_called_once()

    def test_wrong_frequency_and_excess_power_stop_the_test(self):
        for text in ('channel 1 (2412 MHz)\ntxpower 10.00 dBm',
                     'channel 11 (2462 MHz)\ntxpower 13.00 dBm'):
            with self.subTest(text=text),self.assertRaises(RuntimeError):self.radio(text).tune(2462)

    def test_unsupported_or_disabled_backup_is_rejected_before_tuning(self):
        for capabilities in ('* 2412 MHz [1] (20.0 dBm)',
                             '* 2412 MHz [1]\n* 2462 MHz [11] (disabled)',
                             '* 2412 MHz [1]\n* 2462 MHz [11] (no IR)'):
            radio=self.radio('')
            radio.run=Mock(return_value=capabilities)
            with self.assertRaises(ValueError):radio.check_frequencies('wiphy 0')


if __name__=='__main__':unittest.main()
