"""Cross-project localhost test. This never proves radio throughput or range."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--air-repo', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--seconds', type=float, default=60)
    ap.add_argument('--recovery', action='store_true')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    ground_repo = Path(__file__).resolve().parents[1]
    air_port, ground_port, command_input = free_port(), free_port(), free_port()
    if len({air_port, ground_port, command_input}) != 3:raise RuntimeError('Ephemeral port collision; rerun')
    processes, logs = [], []
    try:
        for role, package, repo, rx, peer in (
                ('ground', 'nsy_server', ground_repo, ground_port, air_port),
                ('air', 'bvs_camera', args.air_repo.resolve(), air_port, ground_port)):
            command = [sys.executable, '-m', package + '.telemetry', '--role', role, '--mode', 'udp-test',
                       '--rx-port', str(rx), '--test-peer-port', str(peer), '--seconds', str(args.seconds + 20),
                       '--minimum-seconds', str(args.seconds), '--output', str(args.output.resolve() / role)]
            if role == 'air':command += ['--benchmark-seconds', str(args.seconds)]
            else:command += ['--input-port', str(command_input)]
            if role == 'ground' and args.recovery:command += ['--outage-after', '2', '--outage-seconds', '5']
            environment = os.environ.copy();environment['PYTHONPATH'] = str(repo / 'src')
            log = (args.output / (role + '.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command, env=environment, stdout=log, stderr=log))
        for process in processes:
            if process.wait(timeout=args.seconds + 25):raise RuntimeError('Telemetry child failed; inspect logs')
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate();process.wait(timeout=5)
        for log in logs:log.close()
    ground = json.loads((args.output / 'ground/report.json').read_text())
    air = json.loads((args.output / 'air/report.json').read_text())
    assert not ground['errors'] and not air['errors']
    assert ground['mode'] == air['mode'] == 'udp-test'
    assert not ground['range_confirmed'] and not air['range_confirmed']
    reception = ground['reception']
    assert reception['benchmark_end_received']
    if args.recovery:
        for role in ('air', 'ground'):
            events = [json.loads(line) for line in (args.output / role / 'events.jsonl').read_text().splitlines()]
            assert any(e['event'] == 'channel_changed' for e in events), role + ' did not change channel'
            assert any(e['event'] == 'link_restored' for e in events), role + ' did not recover'
    else:
        assert reception['throughput_passed'], reception
        assert reception['loss_passed'], reception
    print(json.dumps(dict(scope='localhost_only_no_radio', recovery=args.recovery, reception=reception,
                          report=str(args.output / 'ground/report.json')), indent=2))


if __name__ == '__main__':main()
