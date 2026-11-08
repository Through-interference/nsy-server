"""Receiver throughput and resource telemetry, without a GStreamer dependency."""
from collections import deque
import os
from pathlib import Path
import resource
import threading
import time

class RuntimeStats:
    def __init__(self,clock=time.monotonic,cpu=None):
        self.clock=clock
        self.cpu=cpu or (lambda:resource.getrusage(resource.RUSAGE_SELF).ru_utime+resource.getrusage(resource.RUSAGE_SELF).ru_stime)
        self.lock=threading.Lock();self.started=clock();self.incoming=0;self.decoded=0;self.rendered=0
        self.history=deque([(self.started,0,0,0)])
        self.render_intervals=deque(maxlen=300);self.last_render=None
        self.cpu_wall=self.started;self.cpu_time=self.cpu();self.cpu_percent=0.
    def mark(self,kind):
        with self.lock:
            setattr(self,kind,getattr(self,kind)+1)
            if kind=='rendered':
                now=self.clock()
                if self.last_render is not None:self.render_intervals.append(1000*(now-self.last_render))
                self.last_render=now
    def sample(self,queue_ms=0):
        now=self.clock()
        with self.lock:
            self.history.append((now,self.incoming,self.decoded,self.rendered))
            while len(self.history)>2 and self.history[1][0]<=now-3:self.history.popleft()
            first=self.history[0];elapsed=max(.001,now-first[0])
            fps=[(n-old)/elapsed for n,old in zip((self.incoming,self.decoded,self.rendered),first[1:])]
            dt=now-self.cpu_wall
            if dt>=.5:
                current=self.cpu();self.cpu_percent=max(0.,100*(current-self.cpu_time)/dt/max(1,os.cpu_count() or 1))
                self.cpu_time=current;self.cpu_wall=now
            temp=None
            try:
                model=Path('/proc/device-tree/model').read_bytes()
                if b'Raspberry Pi' in model:temp=int(Path('/sys/class/thermal/thermal_zone0/temp').read_text())/1000
            except (OSError,ValueError):pass
            warmed=now-self.started>=4
            overloaded=warmed and (queue_ms>80 or self.cpu_percent>75 or (fps[0]>=5 and fps[1]<fps[0]*.75) or (temp is not None and temp>=78))
            intervals=sorted(self.render_intervals)
            return dict(frame_interval_p95_ms=round(intervals[int((len(intervals)-1)*.95)],1) if intervals else None,frame_gap_max_ms=round(max(intervals),1) if intervals else None,input_fps=round(fps[0],1),decoder_fps=round(fps[1],1),preview_fps=round(fps[2],1),
                        decoder_frames=self.decoded,receiver_cpu_percent=round(self.cpu_percent,1),
                        receiver_temp_c=temp,rx_queue_ms=round(queue_ms,1),receiver_overloaded=bool(overloaded))

def enrich(metric,receiver):
    """Only recognized fields cross the authenticated reverse telemetry stream."""
    import dataclasses
    allowed={'receiver_overloaded','decoder_fps','input_fps','rx_queue_ms','receiver_cpu_percent','receiver_temp_c'}
    values={k:receiver[k] for k in allowed if k in receiver}
    # Inter-frame decode starvation from RF loss must not be mistaken for CPU overload.
    if metric.post_loss>.01 and receiver.get('rx_queue_ms',0)<=80 and receiver.get('receiver_cpu_percent',0)<=75 and (receiver.get('receiver_temp_c') or 0)<78:
        values['receiver_overloaded']=False
    return dataclasses.replace(metric,**values).validate()
