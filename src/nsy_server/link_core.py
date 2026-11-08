"""WFB receive metrics and the version-1 feedback wire format."""
from dataclasses import dataclass
import math

@dataclass
class Metric:
    rssi: float | None
    snr: float | None
    pre_loss: float
    post_loss: float
    active: bool
    source: str = 'wfb'
    receiver_overloaded: bool = False
    decoder_fps: float | None = None
    input_fps: float | None = None
    rx_queue_ms: float | None = None
    receiver_cpu_percent: float | None = None
    receiver_temp_c: float | None = None

    def validate(self):
        if type(self.active) is not bool:raise ValueError('active flag must be boolean')
        for v in (self.pre_loss, self.post_loss):
            if not math.isfinite(v) or not 0 <= v <= 1:
                raise ValueError('loss must be a finite fraction in [0,1]')
        for v, lower, upper in [(self.rssi, -127, 0), (self.snr, -40, 80)]:
            if v is not None and (not math.isfinite(v) or not lower <= v <= upper):
                raise ValueError('invalid radio measurement')
        if type(self.receiver_overloaded) is not bool:raise ValueError('overload flag must be boolean')
        for key,upper in [('decoder_fps',1000),('input_fps',1000),('rx_queue_ms',60000),('receiver_cpu_percent',100),('receiver_temp_c',150)]:
            v=getattr(self,key)
            if v is not None and (not math.isfinite(v) or not 0<=v<=upper):raise ValueError('invalid '+key)
        if self.source not in ('wfb', 'simulation', 'wfb-ip'):
            raise ValueError('unknown telemetry source')
        return self


def normalize_json(msg, stream_id=None):
    """WFB-ng JSON API uses per-interval,total pairs and one row per antenna."""
    if msg.get('type') != 'rx' or (stream_id is not None and msg.get('id') != stream_id):
        return None
    packets = msg['packets']
    def val(key):
        v = packets.get(key, (0,0))
        return int(v[0] if isinstance(v, (list,tuple)) else v)
    return from_counters({k:val(k) for k in ('out','lost','frags','frags_lost')}, msg.get('rx_ant_stats', []))


def from_counters(counters, antennas, source='wfb'):
    if any(v < 0 for v in counters.values()):
        raise ValueError('negative counters')
    valid = [a for a in antennas if a.get('pkt_recv',0)>0 and -127 <= a.get('rssi_avg',-128) <= 0]
    # Strongest populated antenna is eligible; zero-packet RSSI cannot win.
    best = max(valid, key=lambda a:a['rssi_avg']) if valid else None
    snr = best.get('snr_avg') if best else None
    if snr is not None and not -40 <= snr <= 80:
        snr = None
    out, lost = counters['out'], counters['lost']
    frags, missing = counters['frags'], counters['frags_lost']
    return Metric(best['rssi_avg'] if best else None, snr,
                  min(1,missing/frags) if frags else 0,
                  min(1,lost/(out+lost)) if out+lost else 0,
                  bool(out+lost or valid), source).validate()

class RawStats:
    """Parser for current wfb_rx stdout RX_ANT/PKT lines, not stderr."""
    def __init__(self): self.antennas=[]
    def feed(self, line, source='wfb'):
        cols=line.strip().split('\t')
        if len(cols)<2: return None
        if cols[1]=='RX_ANT' and len(cols)==5:
            v=list(map(int,cols[4].split(':')))
            if len(v)!=7: raise ValueError('unsupported RX_ANT format')
            self.antennas.append(dict(pkt_recv=v[0],rssi_avg=v[2],snr_avg=v[5]))
        elif cols[1]=='PKT' and len(cols)==3:
            v=list(map(int,cols[2].split(':')))
            if len(v)!=13: raise ValueError('unsupported wfb_rx PKT format')
            ants,self.antennas=self.antennas,[]
            return from_counters(dict(out=v[9],lost=v[7],frags=v[11],frags_lost=v[12]), ants,source)
        return None
