"""Telemetry v1: useful UDP payloads, checked load and bidirectional channel recovery.

WFB supplies encryption/authentication/FEC. This envelope is not encryption.
Time values used for rates and recovery are local monotonic seconds.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import struct
import zlib

MAX_PAYLOAD = 1100
HEADER = struct.Struct('!4sc16sQHHI')
ECHO = struct.Struct('!16sQ')
ZERO_SESSION = bytes(16)


@dataclass(frozen=True)
class Packet:
    kind: bytes
    session: bytes
    sequence: int
    frequency: int
    payload: bytes = b''


def encode(packet):
    if (packet.kind not in (b'H', b'D', b'B', b'F') or len(packet.session) != 16
            or packet.session == ZERO_SESSION or not 0 <= packet.sequence < 2**64
            or not 2400 <= packet.frequency <= 5900 or len(packet.payload) > MAX_PAYLOAD):
        raise ValueError('Invalid telemetry envelope')
    if packet.kind == b'H' and (not packet.sequence or len(packet.payload) != ECHO.size):
        raise ValueError('Invalid heartbeat')
    if packet.kind in (b'D', b'B') and (not packet.sequence or not packet.payload):
        raise ValueError('Invalid data packet')
    return HEADER.pack(b'TLM1', packet.kind, packet.session, packet.sequence,
                       packet.frequency, len(packet.payload), zlib.crc32(packet.payload)) + packet.payload


def decode(data):
    if len(data) < HEADER.size:
        raise ValueError('Truncated telemetry envelope')
    magic, kind, session, sequence, frequency, length, checksum = HEADER.unpack_from(data)
    payload = data[HEADER.size:]
    if magic != b'TLM1' or length != len(payload) or zlib.crc32(payload) != checksum:
        raise ValueError('Corrupt telemetry envelope')
    packet = Packet(kind, session, sequence, frequency, payload)
    encode(packet)
    return packet


def benchmark_payload(session, sequence, size):
    return hashlib.shake_256(session + sequence.to_bytes(8, 'big')).digest(size)


class LinkRecovery:
    """Fresh ACK progress proves both directions; different scan dwell avoids lockstep."""
    def __init__(self, role, session, frequencies, now, timeout=3):
        if role not in ('air', 'ground') or len(frequencies) < 2 or len(set(frequencies)) != len(frequencies):
            raise ValueError('Two or more distinct channels and a valid role are required')
        self.role, self.session, self.frequencies = role, session, tuple(frequencies)
        self.timeout = timeout
        self.dwell = 1.5 if role == 'air' else 4.5
        self.channel = 0
        self.started = self.switched = now
        self.last_confirmed = None
        self.confirmed_frequency = None
        self.peer = None
        self.peer_sequence = 0
        self.heartbeat_sequence = self.acknowledged = 0
        self.had_link = self.was_connected = False
        self.outage_since = None
        self.events = []

    @property
    def frequency(self):
        return self.frequencies[self.channel]

    def heartbeat(self):
        self.heartbeat_sequence += 1
        return Packet(b'H', self.session, self.heartbeat_sequence, self.frequency,
                      ECHO.pack(self.peer or ZERO_SESSION, self.peer_sequence))

    def receive(self, packet, now):
        if packet.kind != b'H' or packet.frequency != self.frequency or packet.session == self.session:
            return False
        if packet.session != self.peer:
            self.peer = packet.session
            self.peer_sequence = 0
        if packet.sequence <= self.peer_sequence:
            return False
        self.peer_sequence = packet.sequence
        echoed_session, echoed_sequence = ECHO.unpack(packet.payload)
        if (echoed_session == self.session and self.acknowledged < echoed_sequence <= self.heartbeat_sequence
                and self.heartbeat_sequence - echoed_sequence <= 8):
            self.acknowledged = echoed_sequence
            self.last_confirmed = now
            self.confirmed_frequency = self.frequency
        self._record_state(now)
        return True

    def connected(self, now):
        return (self.last_confirmed is not None and self.confirmed_frequency == self.frequency
                and now - self.last_confirmed < self.timeout)

    def _record_state(self, now):
        connected = self.connected(now)
        if connected and not self.was_connected:
            event = dict(event='link_restored' if self.had_link else 'link_up',
                         frequency_mhz=self.frequency, peer_session=self.peer.hex())
            if self.outage_since is not None:
                event['gap_since_last_ack_s'] = now - self.outage_since
            self.events.append(event)
            self.had_link = True
            self.outage_since = None
        elif not connected and self.was_connected:
            self.outage_since = self.last_confirmed
            self.events.append(dict(event='link_down', frequency_mhz=self.frequency))
        self.was_connected = connected

    def next_frequency(self, now):
        self._record_state(now)
        origin = self.last_confirmed if self.last_confirmed is not None else self.started
        if not self.connected(now) and now - origin >= self.timeout and now - self.switched >= self.dwell:
            return self.frequencies[(self.channel + 1) % len(self.frequencies)]
        return None

    def tuned(self, frequency, now):
        previous = self.frequency
        self.channel = self.frequencies.index(frequency)
        self.switched = now
        self.confirmed_frequency = None
        self.peer = None
        self.peer_sequence = 0
        self.events.append(dict(event='channel_changed', from_mhz=previous, to_mhz=frequency))
        self._record_state(now)


class Receiver:
    """Count only unique validated payload bytes, never envelopes, FEC or duplicates."""
    WINDOW = 8192

    def __init__(self):
        self.session = self.kind = None
        self.first_at = self.last_at = self.finish_at = None
        self.highest = self.bitmap = self.packets = self.bytes = self.duplicates = self.too_old = 0
        self.max_gap = 0
        self.finish = None
        self.payload_size = None

    def receive(self, packet, now):
        if packet.kind not in (b'D', b'B'):
            raise ValueError('Expected a telemetry payload')
        if self.session is not None and (packet.session != self.session or packet.kind != self.kind):
            raise ValueError('Telemetry sender/session changed; start a new measurement')
        if packet.kind == b'B' and packet.payload != benchmark_payload(packet.session, packet.sequence, len(packet.payload)):
            raise ValueError('Benchmark pattern mismatch')
        if packet.kind == b'B' and self.payload_size is not None and len(packet.payload) != self.payload_size:
            raise ValueError('Benchmark payload size changed')
        if packet.sequence > self.highest:
            shift = packet.sequence - self.highest
            self.bitmap = 0 if shift >= self.WINDOW else (self.bitmap << shift) & ((1 << self.WINDOW) - 1)
            self.highest = packet.sequence
        offset = self.highest - packet.sequence
        if offset >= self.WINDOW:
            self.too_old += 1
            return False
        if self.bitmap & (1 << offset):
            self.duplicates += 1
            return False
        self.bitmap |= 1 << offset
        self.session, self.kind = packet.session, packet.kind
        self.payload_size = len(packet.payload)
        if self.first_at is None:
            self.first_at = now
        if self.last_at is not None:
            self.max_gap = max(self.max_gap, now - self.last_at)
        self.last_at = now
        self.packets += 1
        self.bytes += len(packet.payload)
        return True

    def end(self, packet, now):
        if packet.kind != b'F' or packet.session != self.session or self.kind != b'B':
            raise ValueError('No matching benchmark stream')
        data = json.loads(packet.payload)
        if not isinstance(data, dict):
            raise ValueError('Invalid benchmark summary')
        for key in ('attempted_packets', 'submitted_packets', 'payload_size'):
            if type(data.get(key)) is not int or data[key] < 1:
                raise ValueError('Invalid benchmark counters')
        duration = data.get('duration_s')
        if (type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0
                or not self.highest <= data['attempted_packets'] or not self.packets <= data['submitted_packets'] <= data['attempted_packets']
                or data['payload_size'] != self.payload_size or data.get('pattern') != 'shake256-v1'):
            raise ValueError('Invalid benchmark duration, pattern or accounting')
        if self.finish is not None and data != self.finish:
            raise ValueError('Conflicting benchmark summaries')
        self.finish = data
        if self.finish_at is None:
            self.finish_at = now

    def report(self, now, target_bps=300000, minimum_seconds=60, max_loss=.01):
        end = max(self.last_at or now, self.finish_at or now) if self.finish is None else max(self.last_at, self.finish_at)
        elapsed = 0 if self.first_at is None else end - self.first_at
        if self.finish:
            elapsed = max(elapsed, self.finish['duration_s'])
        goodput = 8 * self.bytes / elapsed if elapsed > 0 else 0
        expected = self.finish['attempted_packets'] if self.finish else None
        loss = (expected - self.packets) / expected if expected else None
        complete = self.kind == b'D' or self.finish is not None
        passed = bool(complete and elapsed >= minimum_seconds and goodput >= target_bps)
        return dict(sender_session=self.session.hex() if self.session else None,
                    source='generated_load' if self.kind == b'B' else 'live_udp' if self.kind == b'D' else 'no_data',
                    payload_bytes=self.bytes, unique_packets=self.packets, duplicate_packets=self.duplicates,
                    too_old_packets=self.too_old, measurement_seconds=elapsed, payload_goodput_bps=goodput,
                    maximum_interpacket_gap_s=self.max_gap, expected_packets=expected, loss_fraction=loss,
                    benchmark_end_received=self.finish is not None, throughput_passed=passed,
                    loss_passed=loss <= max_loss if loss is not None else None,
                    target_bps=target_bps, minimum_seconds=minimum_seconds, max_loss_fraction=max_loss)
