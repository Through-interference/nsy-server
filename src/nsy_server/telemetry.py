"""Dedicated WFB telemetry and range/recovery evidence; no camera or decoder required."""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import time
import uuid

from .paths import PROJECT_ROOT
from .telemetry_protocol import (MAX_PAYLOAD, Packet, LinkRecovery, Receiver,
                                 benchmark_payload, decode, encode)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def software_identity():
    files = {name: digest(Path(__file__).with_name(name)) for name in ('telemetry.py', 'telemetry_protocol.py')}
    revision = dirty = None
    try:
        result = subprocess.run(['git', '-C', str(PROJECT_ROOT), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            revision = result.stdout.strip()
            status = subprocess.run(['git', '-C', str(PROJECT_ROOT), 'status', '--porcelain'],
                                    capture_output=True, text=True, timeout=2)
            if status.returncode == 0:dirty = bool(status.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return dict(git_commit=revision, working_tree_dirty=dirty, files_sha256=files)


class Journal:
    def __init__(self, directory, role, session, mode):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.stream = (directory / 'events.jsonl').open('w')
        self.identity = dict(role=role, session=session.hex(), mode=mode)

    def write(self, event, **fields):
        row = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   monotonic_s=time.monotonic(), **self.identity, event=event, **fields)
        self.stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.stream.flush()

    def json(self, name, value):
        path = self.directory / name
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        temporary.replace(path)


def read_site(path):
    if path is None:
        return None
    site = json.loads(path.read_text())
    if not isinstance(site, dict):
        raise ValueError('Site metadata must be a JSON object')
    for key, bound in (('latitude', 90), ('longitude', 180)):
        value = site.get(key)
        if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > bound:
            raise ValueError('Invalid site ' + key)
    site['coordinates_source'] = 'operator_supplied_not_independently_verified'
    return site


def commands(args):
    key = args.keys / ('drone.key' if args.role == 'air' else 'gs.key')
    tx_stream, rx_stream = (2, 3) if args.role == 'air' else (3, 2)
    k, n = (4, 8) if args.role == 'air' else (1, 3)
    common = ['-K', str(key.resolve()), '-i', str(args.link_id)]
    tx = [str(args.binary_dir / 'wfb_tx'), *common, '-p', str(tx_stream),
          '-u', str(args.tx_port), '-C', str(args.control_port), '-B', '20',
          '-G', 'long', '-M', '0', '-S', '0', '-L', '0', '-k', str(k), '-n', str(n), '-T', '20', args.interface]
    rx = [str(args.binary_dir / 'wfb_rx'), *common, '-p', str(rx_stream),
          '-c', '127.0.0.1', '-u', str(args.rx_port), '-T', '200', args.interface]
    return [('wfb-tx', tx), ('wfb-rx', rx)]


class Radio:
    def __init__(self, args, journal):
        self.args, self.journal = args, journal
        self.children, self.logs = [], []
        self.previous_type = self.previous_frequency = self.previous_power = None
        self.previous_up = False
        self.lock = None
        self.changed = self.paused = False

    def run(self, command):
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=5).stdout

    def info(self):
        text = self.run(['iw', 'dev', self.args.interface, 'info'])
        frequency = re.search(r'\((\d+) MHz\)', text)
        power = re.search(r'txpower\s+(-?[\d.]+)\s+dBm', text)
        return dict(frequency_mhz=int(frequency[1]) if frequency else None,
                    reported_tx_power_dbm=float(power[1]) if power else None, iw_info=text)

    def prepare(self):
        from .radio_launcher import inspect_interface
        self.previous_type = inspect_interface(self.args.interface)
        import fcntl
        lock_path = '/run/lock/through-interference-telemetry-' + self.args.interface + '.lock'
        self.lock = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        link = json.loads(self.run(['ip', '-json', 'link', 'show', 'dev', self.args.interface]))
        self.previous_up = 'UP' in link[0].get('flags', [])
        for port in (self.args.tx_port, self.args.control_port):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', port))
        before = self.info()
        self.check_frequencies(before['iw_info'])
        if self.previous_type == 'monitor' and not re.search(r'width:\s*20 MHz', before['iw_info']):
            raise ValueError('Existing monitor channel is not HT20; no interface settings changed')
        self.previous_frequency = before['frequency_mhz']
        self.previous_power = before['reported_tx_power_dbm']
        if self.args.tx_power_dbm is not None and self.previous_power is None:
            raise ValueError('Cannot read existing TX power; no interface settings changed')
        self.journal.write('radio_before', **before, regdomain=self.run(['iw', 'reg', 'get']))
        self.changed = True
        self.run(['ip', 'link', 'set', self.args.interface, 'down'])
        self.run(['iw', 'dev', self.args.interface, 'set', 'type', 'monitor'])
        self.run(['ip', 'link', 'set', self.args.interface, 'up'])
        if self.args.tx_power_dbm is not None:
            self.run(['iw', 'dev', self.args.interface, 'set', 'txpower', 'limit', str(round(100 * self.args.tx_power_dbm))])
        self.tune(self.args.frequencies[0])
        for name, command in commands(self.args):
            log = (self.journal.directory / (name + '.log')).open('w')
            self.logs.append(log)
            proc = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
            self.children.append(proc)
            self.journal.write('child_started', name=name, pid=proc.pid, command=command)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.check()
            try:
                response = self.run([str(self.args.binary_dir / 'wfb_tx_cmd'), str(self.args.control_port), 'get_radio'])
                if 'mcs_index' in response:
                    fec = self.run([str(self.args.binary_dir / 'wfb_tx_cmd'), str(self.args.control_port), 'get_fec'])
                    values = dict(line.strip().split('=', 1) for line in (response + '\n' + fec).splitlines() if '=' in line)
                    expected_fec = (4, 8) if self.args.role == 'air' else (1, 3)
                    if (int(values.get('mcs_index', '-1')), int(values.get('bandwidth', '-1')),
                        int(values.get('k', '-1')), int(values.get('n', '-1'))) != (0, 20, *expected_fec):
                        raise RuntimeError('WFB MCS/bandwidth/FEC readback mismatch')
                    self.journal.write('wfb_ready', radio_readback=response, fec_readback=fec)
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(.1)
        raise RuntimeError('WFB TX control startup timeout')

    def check_frequencies(self, info):
        physical = re.search(r'\bwiphy\s+(\d+)', info)
        if physical is None:raise ValueError('Cannot identify the physical radio')
        capabilities = self.run(['iw', 'phy', 'phy' + physical[1], 'info'])
        for frequency in self.args.frequencies:
            row = re.search(r'\*\s+' + str(frequency) + r'\s+MHz\b[^\n]*', capabilities)
            if row is None or any(flag in row[0].lower() for flag in ('disabled', 'no ir', 'radar detection')):
                raise ValueError('Requested channel is unavailable for this TX setup: ' + str(frequency))
        self.journal.write('radio_capabilities', iw_phy_info=capabilities)

    def check(self):
        if any(p.poll() is not None for p in self.children):
            raise RuntimeError('WFB subprocess exited; inspect wfb-tx.log / wfb-rx.log')

    def tune(self, frequency):
        self.run(['iw', 'dev', self.args.interface, 'set', 'freq', str(frequency), 'HT20'])
        state = self.info()
        if state['frequency_mhz'] != frequency:
            raise RuntimeError('Radio frequency readback mismatch')
        power = state['reported_tx_power_dbm']
        if self.args.tx_power_dbm is not None and (power is None or power > self.args.tx_power_dbm + .01):
            raise RuntimeError('Radio power limit readback mismatch')
        self.journal.write('channel_verified', **state)

    def pause(self, paused):
        os.killpg(self.children[0].pid, signal.SIGSTOP if paused else signal.SIGCONT)
        self.paused = paused

    def close(self):
        if self.paused:
            try:self.pause(False)
            except ProcessLookupError:pass
        for process in reversed(self.children):
            if process.poll() is None:
                try:os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:pass
        for process in reversed(self.children):
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in self.logs:
            log.close()
        if self.changed:
            try:
                self.run(['ip', 'link', 'set', self.args.interface, 'down'])
                self.run(['iw', 'dev', self.args.interface, 'set', 'type', self.previous_type])
                self.run(['ip', 'link', 'set', self.args.interface, 'up'])
                if self.previous_type == 'monitor' and self.previous_frequency:
                    self.run(['iw', 'dev', self.args.interface, 'set', 'freq', str(self.previous_frequency), 'HT20'])
                if self.args.tx_power_dbm is not None and self.previous_power is not None:
                    self.run(['iw', 'dev', self.args.interface, 'set', 'txpower', 'limit', str(round(100 * self.previous_power))])
                if not self.previous_up:self.run(['ip', 'link', 'set', self.args.interface, 'down'])
            finally:
                if self.lock:self.lock.close()
        elif self.lock:self.lock.close()


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--role', required=True, choices=('air', 'ground'))
    ap.add_argument('--mode', choices=('radio', 'udp-test'), default='radio')
    ap.add_argument('--interface', help='dedicated monitor/injection interface')
    ap.add_argument('--frequencies', type=int, nargs='+', default=[2412, 2462])
    ap.add_argument('--tx-power-dbm', type=float, help='optional driver power LIMIT; no automatic power increase')
    ap.add_argument('--link-id', type=int, default=1)
    ap.add_argument('--keys', type=Path, default=PROJECT_ROOT / 'keys/drone1')
    ap.add_argument('--binary-dir', type=Path, default=PROJECT_ROOT / 'vendor/skvoz-wfb-ng')
    ap.add_argument('--tx-port', type=int)
    ap.add_argument('--rx-port', type=int)
    ap.add_argument('--control-port', type=int)
    ap.add_argument('--input-port', type=int, help='localhost input: air telemetry 14550 / ground commands 14551')
    ap.add_argument('--output-port', type=int, help='localhost destination: air commands 14551 / ground telemetry 14550')
    ap.add_argument('--test-peer-port', type=int, help='udp-test only; no WFB/radio/encryption')
    ap.add_argument('--benchmark-seconds', type=float, default=0, help='air: generated checked load, not flight data')
    ap.add_argument('--offer-bps', type=int, default=330000, help='generated useful payload offered per second')
    ap.add_argument('--target-bps', type=int, default=300000)
    ap.add_argument('--payload-size', type=int, default=1000)
    ap.add_argument('--minimum-seconds', type=float, default=60)
    ap.add_argument('--max-loss', type=float, default=.01)
    ap.add_argument('--seconds', type=float, default=0, help='overall timeout; 0 means unlimited')
    ap.add_argument('--outage-after', type=float, help='ground: pause owned reverse TX after link-up for a recovery test')
    ap.add_argument('--outage-seconds', type=float, default=5)
    ap.add_argument('--site-json', type=Path, help='operator supplied coordinates/antenna/feed-loss details')
    ap.add_argument('--output', type=Path, help='new evidence directory; existing directories are refused')
    ap.add_argument('--plan', action='store_true')
    return ap


def validate(args):
    for key in ('benchmark_seconds', 'minimum_seconds', 'seconds', 'outage_seconds'):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) < 0:
            raise ValueError('Invalid ' + key)
    if args.minimum_seconds <= 0 or args.outage_seconds <= 0:
        raise ValueError('Measurement and outage duration must be positive')
    if args.outage_after is not None and (args.role != 'ground' or not math.isfinite(args.outage_after) or args.outage_after < 0):
        raise ValueError('Outage injection is available only on ground, after a nonnegative delay')
    if args.benchmark_seconds and args.role != 'air':
        raise ValueError('Only air generates the benchmark stream')
    if not 1 <= args.payload_size <= MAX_PAYLOAD or not 1 <= args.target_bps <= args.offer_bps <= 10000000:
        raise ValueError('Invalid payload size or useful bit rate')
    if not math.isfinite(args.max_loss) or not 0 <= args.max_loss <= 1:
        raise ValueError('Loss must be a finite fraction')
    if len(args.frequencies) < 2 or len(set(args.frequencies)) != len(args.frequencies) or any(not 2400 <= f <= 5900 for f in args.frequencies):
        raise ValueError('Use two or more distinct, locally permitted supported frequencies')
    if not 1 <= args.link_id <= 0xffffff:
        raise ValueError('Invalid WFB link ID')
    if args.tx_power_dbm is not None and (not math.isfinite(args.tx_power_dbm) or not -10 <= args.tx_power_dbm <= 30):
        raise ValueError('Invalid requested TX power limit')
    defaults = (14600, 14611, 9011) if args.role == 'air' else (14610, 14601, 9111)
    for key, value in zip(('tx_port', 'rx_port', 'control_port'), defaults):
        if getattr(args, key) is None:setattr(args, key, value)
    if args.input_port is None:args.input_port = 14550 if args.role == 'air' else 14551
    if args.output_port is None:args.output_port = 14551 if args.role == 'air' else 14550
    for key in ('tx_port', 'rx_port', 'control_port', 'input_port', 'output_port', 'test_peer_port'):
        port = getattr(args, key)
        if port is not None and not 1024 <= port <= 65535:
            raise ValueError('Invalid ' + key)
    ports = [args.tx_port, args.rx_port, args.control_port, args.input_port, args.output_port]
    if len(ports) != len(set(ports)):
        raise ValueError('Local telemetry ports must be distinct')
    if args.mode == 'udp-test' and (args.test_peer_port is None or args.tx_power_dbm is not None):
        raise ValueError('udp-test needs --test-peer-port and cannot configure TX power')
    if args.mode == 'radio' and (not args.interface or args.test_peer_port is not None):
        raise ValueError('Radio needs --interface and does not use --test-peer-port')
    return args


def main(argv=None):
    ap = parser()
    try:
        args = validate(ap.parse_args(argv))
        site = read_site(args.site_json)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    if args.plan:
        print(json.dumps(dict(role=args.role, mode=args.mode, target_payload_bps=args.target_bps,
                             frequencies=args.frequencies, processes=commands(args) if args.mode == 'radio' else [],
                             radio_range_confirmed=False), indent=2))
        return 0
    if args.mode == 'radio':
        if os.geteuid() != 0:ap.error('Dedicated radio setup requires root; --plan is read-only')
        for name in ('wfb_rx', 'wfb_tx', 'wfb_tx_cmd'):
            if not os.access(args.binary_dir / name, os.X_OK):ap.error('Missing executable: ' + name)
        key = args.keys / ('drone.key' if args.role == 'air' else 'gs.key')
        if not key.is_file() or not key.stat().st_size:ap.error('Matching WFB key is required')
    session = uuid.uuid4().bytes
    directory = args.output or PROJECT_ROOT / 'results/telemetry' / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + session.hex()[:8])
    journal = Journal(directory, args.role, session, args.mode)
    radio = Radio(args, journal) if args.mode == 'radio' else None
    receiver, sock, source = Receiver(), None, None
    manifest = dict(protocol='TLM1', role=args.role, session=session.hex(), mode=args.mode,
                    interface=args.interface, frequencies_mhz=args.frequencies, link_id=args.link_id,
                    target_bps=args.target_bps, minimum_seconds=args.minimum_seconds,
                    requested_tx_power_limit_dbm=args.tx_power_dbm,
                    software=software_identity(),
                    max_loss_fraction=args.max_loss, source='generated_load' if args.benchmark_seconds else 'live_udp' if args.role == 'air' else 'receive_only',
                    site=site, range_confirmed=False, binaries={})
    if radio:
        manifest['binaries'] = {name: digest(args.binary_dir / name) for name in ('wfb_rx', 'wfb_tx', 'wfb_tx_cmd')}
    journal.json('manifest.json', manifest)
    start = time.monotonic()
    link = LinkRecovery(args.role, session, args.frequencies, start)
    stopped = False
    def stop(*_args):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    tx_sequence = attempted = submitted = local_errors = invalid = forwarded = 0
    bench_start = bench_end = None
    next_data = next_heartbeat = next_status = 0
    first_connected = None
    outage_done = paused = False
    resume_at = None
    next_finish = finish_until = None
    final_payload = None
    errors = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.bind(('127.0.0.1', args.rx_port))
        sock.setblocking(False)
        if args.role == 'ground' or not args.benchmark_seconds:
            source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            source.bind(('127.0.0.1', args.input_port));source.setblocking(False)
        if radio:radio.prepare()
        start = time.monotonic()
        link.started = link.switched = start
        destination = ('127.0.0.1', args.tx_port if radio else args.test_peer_port)

        def send(packet):
            nonlocal local_errors
            if paused:return False
            try:
                sock.sendto(encode(packet), destination)
                return True
            except OSError as exc:
                local_errors += 1
                journal.write('local_send_error', error=str(exc))
                return False

        while not stopped and (not args.seconds or time.monotonic() - start < args.seconds):
            now = time.monotonic()
            if radio:radio.check()
            if link.connected(now) and first_connected is None:first_connected = now
            if args.outage_after is not None and first_connected is not None and not outage_done and now - first_connected >= args.outage_after:
                if radio:radio.pause(True)
                paused = outage_done = True;resume_at = now + args.outage_seconds
                journal.write('outage_injected', mechanism='pause_owned_wfb_tx' if radio else 'suppress_udp_test_tx', duration_s=args.outage_seconds)
            if paused and now >= resume_at:
                if radio:radio.pause(False)
                paused = False
                journal.write('outage_released')
            frequency = link.next_frequency(now)
            while link.events:
                event = link.events.pop(0)
                journal.write(event.pop('event'), **event)
            if frequency is not None:
                if radio:radio.tune(frequency)
                link.tuned(frequency, time.monotonic())
            while link.events:
                event = link.events.pop(0)
                journal.write(event.pop('event'), **event)
            if now >= next_heartbeat:
                send(link.heartbeat());next_heartbeat = now + .25
            if args.benchmark_seconds and bench_start is None and link.connected(now):
                bench_start = next_data = now
                journal.write('benchmark_started', offer_payload_bps=args.offer_bps, payload_size=args.payload_size,
                              duration_s=args.benchmark_seconds)
            if bench_start is not None and bench_end is None:
                if now - bench_start < args.benchmark_seconds:
                    for _ in range(4):
                        if now < next_data:break
                        tx_sequence += 1;attempted += 1
                        payload = benchmark_payload(session, tx_sequence, args.payload_size)
                        ok = send(Packet(b'B', session, tx_sequence, link.frequency, payload))
                        submitted += int(ok)
                        journal.write('payload_sent', sequence=tx_sequence, bytes=len(payload), submitted=ok,
                                      sha256=hashlib.sha256(payload).hexdigest(), frequency_mhz=link.frequency)
                        next_data += 8 * args.payload_size / args.offer_bps
                else:
                    bench_end = now
                    final_payload = json.dumps(dict(attempted_packets=attempted, submitted_packets=submitted,
                        payload_size=args.payload_size, duration_s=bench_end - bench_start, pattern='shake256-v1')).encode()
                    next_finish, finish_until = now, now + 3
                    journal.write('benchmark_finished', **json.loads(final_payload))
            if final_payload is not None and now >= next_finish:
                send(Packet(b'F', session, 0, link.frequency, final_payload));next_finish = now + .25
            if finish_until is not None and now >= finish_until:break
            if args.role == 'ground' and receiver.finish_at is not None and now - max(receiver.finish_at, receiver.last_at) > 1:
                break
            if now >= next_status:
                sample_at = time.monotonic()
                sample = dict(elapsed_s=sample_at-start, connected=link.connected(sample_at), frequency_mhz=link.frequency,
                    peer_session=link.peer.hex() if link.peer else None,
                    attempted_packets=attempted, submitted_packets=submitted, forwarded_packets=forwarded,
                    invalid_packets=invalid, local_send_errors=local_errors,
                    reception=receiver.report(sample_at, args.target_bps, args.minimum_seconds, args.max_loss))
                journal.json('status.json', dict(mode=args.mode, **sample))
                journal.write('sample', **sample)
                next_status = sample_at + 1
            readable, _, _ = select.select([sock] + ([source] if source else []), [], [], .01)
            for endpoint in readable:
                if endpoint is source:
                    payload = source.recv(65535)
                    if not 0 < len(payload) <= MAX_PAYLOAD:
                        journal.write('input_rejected', bytes=len(payload));invalid += 1;continue
                    tx_sequence += 1
                    attempted += 1
                    ok = send(Packet(b'D', session, tx_sequence, link.frequency, payload))
                    submitted += int(ok)
                    journal.write('payload_sent', sequence=tx_sequence, bytes=len(payload), submitted=ok,
                                  sha256=hashlib.sha256(payload).hexdigest(), frequency_mhz=link.frequency)
                    continue
                for _ in range(64):
                    try:data = sock.recv(65535)
                    except BlockingIOError:break
                    try:
                        packet = decode(data)
                        received_at = time.monotonic()
                        if packet.frequency != link.frequency:continue
                        if packet.kind == b'H':
                            link.receive(packet, received_at)
                        elif packet.session == link.peer:
                            if packet.kind == b'D' or (args.role == 'ground' and packet.kind == b'B'):
                                if receiver.session is not None and receiver.session != packet.session:
                                    journal.write('sender_restarted', previous_reception=receiver.report(received_at))
                                    receiver = Receiver()
                                if receiver.receive(packet, received_at):
                                    journal.write('payload_received', sender_session=packet.session.hex(),
                                        sequence=packet.sequence, bytes=len(packet.payload), frequency_mhz=packet.frequency,
                                        source='generated_load' if packet.kind == b'B' else 'live_udp',
                                        sha256=hashlib.sha256(packet.payload).hexdigest())
                                    if packet.kind == b'D':
                                        sock.sendto(packet.payload, ('127.0.0.1', args.output_port));forwarded += 1
                            elif args.role == 'ground' and packet.kind == b'F':receiver.end(packet, received_at)
                    except (ValueError, TypeError, KeyError) as exc:
                        invalid += 1;journal.write('packet_rejected', error=str(exc))
        journal.write('stopped')
    except KeyboardInterrupt:
        journal.write('interrupted')
    except Exception as exc:
        errors.append(str(exc));journal.write('failure', error=str(exc))
    finally:
        measurement_end = time.monotonic()
        for endpoint in (source, sock):
            if endpoint:endpoint.close()
        if radio:
            try:radio.close()
            except Exception as exc:errors.append('cleanup: ' + str(exc))
        journal.stream.close()
        report = dict(**manifest, errors=errors, forwarded_packets=forwarded, invalid_packets=invalid,
                      local_send_errors=local_errors, attempted_packets=attempted, submitted_packets=submitted,
                      reception=receiver.report(measurement_end, args.target_bps, args.minimum_seconds, args.max_loss),
                      evidence_sha256={p.name: digest(p) for p in directory.iterdir() if p.is_file() and p.suffix != '.tmp'})
        journal.json('report.json', report)
    print(json.dumps(dict(report=str(directory / 'report.json'), role=args.role, mode=args.mode,
                          payload_goodput_bps=report['reception']['payload_goodput_bps'], errors=errors)), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
