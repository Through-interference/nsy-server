"""Pure protocol/recovery/rate checks; localhost integration is a separate test."""
import json
import unittest
from nsy_server.telemetry_protocol import (Packet, LinkRecovery, Receiver, benchmark_payload,
                                        decode, encode)
from nsy_server.telemetry import commands, parser, validate

A, G = b'a' * 16, b'g' * 16


class TelemetryTests(unittest.TestCase):
    def test_binary_payload_roundtrip_and_corruption(self):
        p = Packet(b'D', A, 1, 2412, bytes(range(256)))
        self.assertEqual(decode(encode(p)), p)
        damaged = bytearray(encode(p));damaged[-1] ^= 1
        with self.assertRaises(ValueError):decode(damaged)
        for data in (b'', b'TLM1', encode(p)[:-1], encode(p) + b'x'):
            with self.assertRaises(ValueError):decode(data)

    def test_goodput_counts_unique_payload_and_excludes_overhead(self):
        rx = Receiver()
        packets = 2475
        for sequence in range(1, packets + 1):
            p = Packet(b'B', A, sequence, 2412, benchmark_payload(A, sequence, 1000))
            now = (sequence - 1) * 60 / packets
            self.assertTrue(rx.receive(p, now))
            self.assertFalse(rx.receive(p, now))
        self.finish(rx, packets, 60)
        report = rx.report(70)
        self.assertEqual(report['payload_bytes'], packets * 1000)
        self.assertEqual(report['duplicate_packets'], packets)
        self.assertAlmostEqual(report['payload_goodput_bps'], 330000)
        self.assertTrue(report['throughput_passed'])
        self.assertTrue(report['loss_passed'])

    def finish(self, rx, packets, duration, size=1000):
        body = dict(attempted_packets=packets, submitted_packets=packets,
                    payload_size=size, duration_s=duration, pattern='shake256-v1')
        rx.end(Packet(b'F', A, 0, 2412, json.dumps(body).encode()), duration)

    def test_losses_and_short_tests_cannot_prove_target(self):
        rx = Receiver()
        for sequence in range(1, 2476, 2):
            rx.receive(Packet(b'B', A, sequence, 2412, benchmark_payload(A, sequence, 1000)), sequence * 60 / 2475)
        self.finish(rx, 2475, 60)
        self.assertFalse(rx.report(65)['throughput_passed'])
        self.assertFalse(rx.report(65)['loss_passed'])
        short = Receiver()
        short.receive(Packet(b'B', A, 1, 2412, benchmark_payload(A, 1, 1000)), 0)
        self.finish(short, 1, .001)
        self.assertFalse(short.report(1)['throughput_passed'])

    def test_missing_end_and_wrong_pattern_are_not_accepted(self):
        rx = Receiver()
        with self.assertRaises(ValueError):rx.receive(Packet(b'B', A, 1, 2412, b'not-the-pattern'), 0)
        rx.receive(Packet(b'B', A, 1, 2412, benchmark_payload(A, 1, 1000)), 0)
        self.assertFalse(rx.report(60)['throughput_passed'])
        with self.assertRaises(ValueError):self.finish(rx, 1, float('nan'))
        with self.assertRaises(ValueError):self.finish(rx, 1, 60, size=500)

    def test_reordering_and_old_packets_do_not_inflate_rate(self):
        rx = Receiver()
        for sequence in (1, 3, 2):
            self.assertTrue(rx.receive(Packet(b'D', A, sequence, 2412, b'data'), sequence))
        self.assertFalse(rx.receive(Packet(b'D', A, 2, 2412, b'data'), 5))
        rx.receive(Packet(b'D', A, 9000, 2412, b'data'), 6)
        self.assertFalse(rx.receive(Packet(b'D', A, 1, 2412, b'data'), 7))
        self.assertEqual(rx.report(8)['payload_bytes'], 16)

    def pair(self):
        return (LinkRecovery('air', A, [2412, 2462], 0),
                LinkRecovery('ground', G, [2412, 2462], 0))

    def simulate(self, a, g, seconds, blocked=lambda _t, _f: False, one_way=False):
        next_h = 0
        for step in range(int(seconds * 20) + 1):
            now = step / 20
            for link in (a, g):
                frequency = link.next_frequency(now)
                if frequency is not None:link.tuned(frequency, now)
            if now >= next_h:
                ah, gh = a.heartbeat(), g.heartbeat()
                if a.frequency == g.frequency and not blocked(now, a.frequency):
                    g.receive(ah, now)
                    if not one_way:a.receive(gh, now)
                next_h = now + .25

    def test_primary_loss_recovers_on_backup_with_both_directions(self):
        a, g = self.pair()
        self.simulate(a, g, 14, blocked=lambda t, f: t > 2 and f == 2412)
        self.assertTrue(a.connected(14) and g.connected(14))
        self.assertEqual((a.frequency, g.frequency), (2462, 2462))
        for link in (a, g):
            events = [row['event'] for row in link.events]
            self.assertIn('link_down', events)
            self.assertIn('channel_changed', events)
            self.assertIn('link_restored', events)

    def test_mismatched_starting_channels_can_meet(self):
        a, g = self.pair();a.tuned(2462, 0)
        self.simulate(a, g, 15)
        self.assertTrue(a.connected(15) and g.connected(15))

    def test_one_way_traffic_does_not_prove_bidirectional_link(self):
        a, g = self.pair()
        self.simulate(a, g, 10, one_way=True)
        self.assertFalse(a.connected(10) or g.connected(10))

    def test_stale_heartbeat_acks_do_not_keep_link_up(self):
        a, g = self.pair()
        self.simulate(a, g, 1)
        self.assertTrue(a.connected(1))
        for step in range(2, 22):
            now = step / 4
            a.heartbeat()
            a.receive(g.heartbeat(), now)
        self.assertFalse(a.connected(5.5))

    def test_plan_is_camera_free_and_stream_ids_are_separate(self):
        for role, tx, rx in (('air', '2', '3'), ('ground', '3', '2')):
            args = validate(parser().parse_args(['--role', role, '--interface', 'radio1', '--plan']))
            rows = commands(args)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1][rows[0][1].index('-p') + 1], tx)
            self.assertEqual(rows[1][1][rows[1][1].index('-p') + 1], rx)
            self.assertNotIn('camera', str(rows).lower().replace('bvs-camera', ''))

    def test_invalid_rate_and_port_plans_are_rejected(self):
        invalid = [ ['--offer-bps', '299999'], ['--tx-port', '14611'],
                    ['--minimum-seconds', 'nan'], ['--frequencies', '2412', '2412'],
                    ['--benchmark-seconds', 'inf'], ['--max-loss', 'nan'] ]
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate(parser().parse_args(['--role', 'air', '--interface', 'radio1', *extra]))


if __name__ == '__main__':unittest.main()
