"""Client-initiated bidirectional UDP carrier for encrypted WFB debug frames.

This is an IP-network transport, not monitor/injection radio. WFB supplies
payload encryption/FEC; the outer carrier restricts peers by configured IP.
"""
import argparse
import json
from pathlib import Path
import select
import signal
import socket
import threading
import time

MAGIC=b'SKZ1'

def encode(kind,payload=b''):
    if kind not in (b'H',b'A',b'D') or len(payload)>8192 or (kind!=b'D' and payload) or (kind==b'D' and not payload):
        raise ValueError('Invalid carrier datagram')
    return MAGIC+kind+payload

def decode(data):
    if len(data)<5 or data[:4]!=MAGIC:raise ValueError('Unknown carrier protocol')
    kind,payload=data[4:5],data[5:]
    encode(kind,payload)
    return kind,payload

class Session:
    def __init__(self,peer,ttl=5):self.peer=peer;self.ttl=ttl;self.address=None;self.updated=None
    def accept(self,address,now):
        if address[0]!=self.peer:return False
        self.address=address;self.updated=now;return True
    def destination(self,now):
        return self.address if self.updated is not None and now-self.updated<=self.ttl else None

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--role',choices=('server','client'),required=True)
    ap.add_argument('--bind',required=True);ap.add_argument('--peer',required=True)
    ap.add_argument('--port',type=int,default=17030)
    ap.add_argument('--local-input',type=int,required=True);ap.add_argument('--local-output',type=int,required=True)
    ap.add_argument('--status',type=Path);ap.add_argument('--seconds',type=float,default=0)
    args=ap.parse_args();stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    local=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);network=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    for sock in (local,network):
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4194304);sock.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4194304);sock.setblocking(False)
    local.bind(('127.0.0.1',args.local_input));network.bind((args.bind,args.port if args.role=='server' else 0))
    if args.role=='client':network.connect((args.peer,args.port))
    session=Session(args.peer);start=time.monotonic();next_hello=next_status=0;last_peer=None
    counts=dict(sent=0,received=0,errors=0,no_peer_drops=0)
    try:
        while not stop.is_set() and (not args.seconds or time.monotonic()-start<args.seconds):
            now=time.monotonic()
            if args.role=='client' and now>=next_hello:
                try:network.send(encode(b'H'))
                except OSError:counts['errors']+=1
                next_hello=now+.5
            if args.status and now>=next_status:
                state=dict(mode='wfb-ip',role=args.role,connected=last_peer is not None and now-last_peer<5,
                           bind=args.bind,peer=args.peer,**counts)
                args.status.parent.mkdir(parents=True,exist_ok=True)
                tmp=args.status.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.replace(args.status)
                next_status=now+1
            readable,_,_=select.select([local,network],[],[],.1)
            for sock in readable:
                try:data,address=sock.recvfrom(8200)
                except OSError:counts['errors']+=1;continue
                if sock is local:
                    destination=session.destination(now) if args.role=='server' else (args.peer,args.port)
                    if not destination:counts['no_peer_drops']+=1;continue
                    try:network.sendto(encode(b'D',data),destination);counts['sent']+=1
                    except (OSError,ValueError):counts['errors']+=1
                else:
                    try:kind,payload=decode(data)
                    except ValueError:counts['errors']+=1;continue
                    if args.role=='server':
                        if not session.accept(address,now):counts['errors']+=1;continue
                        if kind==b'H':
                            try:network.sendto(encode(b'A'),address)
                            except OSError:counts['errors']+=1
                    last_peer=now
                    if kind==b'D':
                        try:local.sendto(payload,('127.0.0.1',args.local_output));counts['received']+=1
                        except OSError:counts['errors']+=1
    except KeyboardInterrupt:pass
    finally:
        local.close();network.close();print(json.dumps(dict(mode='wfb-ip',**counts)),flush=True)
if __name__=='__main__':main()
