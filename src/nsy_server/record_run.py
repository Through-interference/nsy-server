#!/usr/bin/env python3
"""Log a labelled bench/field run; never invent latency, distance or radio RSSI."""
import argparse,datetime,json,time,urllib.request
from pathlib import Path

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--api',default='http://127.0.0.1:18090');ap.add_argument('--seconds',type=float,default=60)
 ap.add_argument('--distance-m',type=float);ap.add_argument('--notes',default='')
 ap.add_argument('--output',required=True)
 args=ap.parse_args()
 if args.seconds<=0 or args.distance_m is not None and args.distance_m<0:ap.error('invalid duration/distance')
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));until=time.monotonic()+args.seconds
 with Path(args.output).open('a') as f:
  while time.monotonic()<until:
   row=dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(),distance_m=args.distance_m,notes=args.notes)
   try:
    with op.open(args.api+'/status',timeout=10) as response:s=json.load(response)
    if s['mode']=='emulation' and args.distance_m is not None:raise ValueError('Do not label a UDP emulation as a real range measurement')
    row['status']=s
   except Exception as exc:row['error']=str(exc)
   f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush();time.sleep(1)
if __name__=='__main__':main()
