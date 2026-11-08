#!/usr/bin/env python3
"""Return ground RX telemetry via a WFB reverse stream or an existing localhost SSH tunnel."""
import argparse,json,socket,sys,time,urllib.request
from dataclasses import asdict,replace
from .link_core import RawStats,normalize_json,Metric

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--stats-host',default='127.0.0.1');ap.add_argument('--stats-port',type=int,default=8103)
    ap.add_argument('--stream-id',default='video rx');ap.add_argument('--raw',action='store_true')
    ap.add_argument('--udp-port',type=int,default=5800,help='send normalized metrics to local WFB reverse-stream TX')
    ap.add_argument('--receiver-status',help='optional localhost receiver HTTP status URL')
    ap.add_argument('--local-status',help='localhost receiver /link endpoint for operator statistics')
    ap.add_argument('--source',choices=('wfb','wfb-ip'),default='wfb')
    args=ap.parse_args();opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw=RawStats()
    outgoing=socket.socket(socket.AF_INET,socket.SOCK_DGRAM) if args.udp_port else None
    while True:
        try:
            conn=None
            if args.raw:stream=sys.stdin
            else:
                conn=socket.create_connection((args.stats_host,args.stats_port),timeout=5);conn.settimeout(4)
                stream=conn.makefile('r')
            for line in stream:
                try:
                    m=raw.feed(line,source=args.source) if args.raw else normalize_json(json.loads(line),args.stream_id)
                    if m is None:continue
                    if m.source=='wfb-ip':m=replace(m,rssi=None,snr=None)
                    if args.receiver_status:
                        try:
                            from .runtime_stats import enrich
                            with opener.open(args.receiver_status,timeout=.3) as response:m=enrich(m,json.load(response))
                        except (OSError,ValueError):pass
                    if args.local_status:
                        try:
                            local=urllib.request.Request(args.local_status,data=json.dumps(asdict(m)).encode(),headers={'Content-Type':'application/json'})
                            with opener.open(local,timeout=.2) as response:response.read()
                        except (OSError,ValueError):pass
                    if outgoing:
                        outgoing.sendto(json.dumps(asdict(m)).encode(),('127.0.0.1',args.udp_port))
                        continue
                except (ValueError,KeyError,TypeError) as exc:print('Rejected telemetry:',exc,file=sys.stderr)
            if args.raw:return
        except (OSError,TimeoutError) as exc:print('Feedback unavailable:',exc,file=sys.stderr)
        finally:
            if 'conn' in locals() and conn:conn.close()
        time.sleep(1)
if __name__=='__main__':main()
