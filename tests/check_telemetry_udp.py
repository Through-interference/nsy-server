"""Check live UDP payload forwarding between the two projects without RF."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def free_port():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def check_peer_filter(args):
    """Configure a nonmatching allow-list; no secondary loopback alias is needed."""
    directory=args.output.resolve()/'peer-filter'
    rx,input_port=free_port(),free_port()
    environment=os.environ.copy();environment['PYTHONPATH']=str(args.air_repo.resolve()/'src')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as peer,socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender,(args.output/'peer-filter.log').open('w') as log:
        peer.bind(('127.0.0.1',0))
        command=[sys.executable,'-m','bvs_camera.telemetry','--role','air','--mode','udp-test',
                 '--rx-port',str(rx),'--test-peer-port',str(peer.getsockname()[1]),
                 '--input-port',str(input_port),'--input-peer','127.0.0.2','--seconds','2','--output',str(directory)]
        child=subprocess.Popen(command,env=environment,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+2
            while not (directory/'status.json').exists() and time.monotonic()<deadline:time.sleep(.01)
            assert (directory/'status.json').exists(),'Peer-filter fixture did not start'
            sender.sendto(b'reject-this-peer',('127.0.0.1',input_port))
            assert child.wait(timeout=5)==0
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
    report=json.loads((directory/'report.json').read_text())
    assert report['invalid_packets']>=1
    assert report['attempted_packets']==0


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--air-repo',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    ports=[free_port() for _ in range(6)]
    if len(set(ports))!=6:raise RuntimeError('Ephemeral port collision; rerun')
    air_rx,ground_rx,source_port,output_port,command_input,command_output=ports
    ground_repo=Path(__file__).resolve().parents[1]
    children=[];logs=[]
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sink,socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as source,socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as command_sink:
        sink.bind(('127.0.0.1',output_port));sink.settimeout(2)
        command_sink.bind(('127.0.0.1',command_output));command_sink.settimeout(2)
        try:
            for role,package,repo,rx,peer in [('ground','nsy_server',ground_repo,ground_rx,air_rx),
                                            ('air','bvs_camera',args.air_repo.resolve(),air_rx,ground_rx)]:
                command=[sys.executable,'-m',package+'.telemetry','--role',role,'--mode','udp-test',
                         '--rx-port',str(rx),'--test-peer-port',str(peer),'--seconds','5',
                         '--input-port',str(source_port if role=='air' else command_input),
                         '--input-peer','127.0.0.1',
                         '--output-port',str(command_output if role=='air' else output_port),
                         '--output',str(args.output.resolve()/role)]
                environment=os.environ.copy();environment['PYTHONPATH']=str(repo/'src')
                log=(args.output/(role+'.log')).open('w');logs.append(log)
                children.append(subprocess.Popen(command,env=environment,stdout=log,stderr=log))
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    if all(json.loads((args.output/role/'status.json').read_text())['connected'] for role in ('air','ground')):break
                except (OSError,ValueError):pass
                time.sleep(.05)
            else:raise AssertionError('Telemetry heartbeat handshake did not complete')
            payloads=[b'\x00\xff\r\n',bytes(range(256))*4,os.urandom(1100),b'x']*5
            for payload in payloads:source.sendto(payload,('127.0.0.1',source_port))
            received=[sink.recv(4096) for _ in payloads]
            assert received==payloads,'Live UDP payloads changed or were lost'
            commands=[b'command\x00\xff'+bytes([i]) for i in range(20)]
            for command in commands:source.sendto(command,('127.0.0.1',command_input))
            assert [command_sink.recv(4096) for _ in commands]==commands,'Reverse UDP commands changed or were lost'
            for child in children:assert child.wait(timeout=7)==0
        finally:
            for child in children:
                if child.poll() is None:child.terminate();child.wait(timeout=5)
            for log in logs:log.close()
    report=json.loads((args.output/'ground/report.json').read_text())
    assert report['forwarded_packets']==len(payloads)
    air_report=json.loads((args.output/'air/report.json').read_text())
    assert air_report['forwarded_packets']==len(commands)
    assert report['reception']['source']=='live_udp'
    assert not report['range_confirmed']
    check_peer_filter(args)
    print(json.dumps(dict(scope='localhost_only_no_radio',forwarded_packets=len(payloads),
                          reverse_commands_preserved=len(commands),
                          peer_filter_rejection_verified=True,
                          payload_bytes=sum(map(len,payloads)),payloads_preserved=True)))


if __name__=='__main__':main()
