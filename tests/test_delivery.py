#!/usr/bin/env python3
"""Measure JPEG delivery stalls; does not measure browser paint or camera-to-screen latency."""
import argparse,json,time,statistics,urllib.request
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--url',default='http://127.0.0.1:18091/stream.mjpg');ap.add_argument('--seconds',type=float,default=20)
    ap.add_argument('--max-gap-ms',type=float,default=100);ap.add_argument('--output',default='results/review-delivery.json')
    args=ap.parse_args()
    if args.seconds<5 or args.max_gap_ms<=0:ap.error('duration >=5 and gap >0 required')
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}));times=[];total=0
    with op.open(args.url,timeout=8) as stream:
        start=time.monotonic()
        while time.monotonic()-start<args.seconds:
            line=stream.readline()
            if not line:raise RuntimeError('Unexpected end of video')
            if not line.startswith(b'--frame'):continue
            size=None
            while True:
                line=stream.readline()
                if not line:raise RuntimeError('Truncated frame header')
                if line==b'\r\n':break
                if line.lower().startswith(b'content-length:'):size=int(line.split(b':',1)[1])
            if size is None or not 0<size<=4194304:raise ValueError('Invalid JPEG size')
            frame=stream.read(size)
            if len(frame)!=size or not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9'):raise ValueError('Invalid/truncated JPEG')
            now=time.monotonic()
            if now-start>2:times.append(now);total+=size
    if len(times)<2:raise AssertionError('Not enough new frames')
    intervals=sorted(1000*(b-a) for a,b in zip(times,times[1:]));elapsed=times[-1]-times[0]
    result=dict(passed=max(intervals)<=args.max_gap_ms,scope='HTTP JPEG delivery only',frames=len(times),fps=(len(times)-1)/elapsed,
                jpeg_mbps=8*total/elapsed/1e6,max_gap_ms=max(intervals),allowed_max_gap_ms=args.max_gap_ms,
                interval_p95_ms=intervals[int(len(intervals)*.95)],interval_p99_ms=intervals[int(len(intervals)*.99)],
                gaps_over_limit=sum(i>args.max_gap_ms for i in intervals))
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    if not result['passed']:raise SystemExit(1)
if __name__=='__main__':main()
