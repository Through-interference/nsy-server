#!/usr/bin/env python3
"""Validate/render independent FDMA channels, or supervise ground RX processes."""
import argparse,json,re,subprocess,sys,time,signal
from pathlib import Path
from .paths import DEFAULT_CONFIG,module_command,child_env

def validate(config):
    bw=config['bandwidth_mhz']
    if bw!=20:raise ValueError('This implementation uses 20 MHz HT channels')
    drones=config['drones']
    if not 1<=len(drones)<=3:raise ValueError('Use 1 to 3 drones')
    for i,d in enumerate(drones):d.setdefault('gs_status_port',8091+2*i)
    for key in ['id','link_id','frequency_mhz','gs_interface','gs_video_port','gs_feedback_port','gs_control_port','air_api_port','gs_status_port']:
        values=[d[key] for d in drones]
        if len(set(values))!=len(values):raise ValueError('Duplicate '+key)
    for d in drones:
        if not re.fullmatch('[A-Za-z0-9_.-]+',d['id']):raise ValueError('Invalid id')
        if not re.fullmatch('[A-Za-z0-9_.-]+',d['gs_interface']):raise ValueError('Invalid interface')
        if not isinstance(d['frequency_mhz'],int) or not 2400<=d['frequency_mhz']<=2500:raise ValueError('Invalid frequency')
        if not isinstance(d['link_id'],int) or not 1<=d['link_id']<=0xffffff:raise ValueError('Invalid link id')
        for key in ['gs_video_port','gs_feedback_port','gs_control_port','air_api_port','gs_status_port']:
            if not isinstance(d[key],int) or not 1024<=d[key]<=65535:raise ValueError('Invalid port')
    for i,a in enumerate(drones):
        for b in drones[i+1:]:
            if abs(a['frequency_mhz']-b['frequency_mhz'])<bw+5:raise ValueError('Overlapping channels or insufficient 5 MHz guard')
    ports=[d[key] for d in drones for key in ['gs_video_port','gs_feedback_port','gs_control_port']]
    if len(ports)!=len(set(ports)):raise ValueError('UDP video, feedback and control ports must be distinct')
    return config

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',default=str(DEFAULT_CONFIG))
    ap.add_argument('--run-ground',action='store_true');ap.add_argument('--binary-dir',default='.')
    ap.add_argument('--keys',default='keys')
    args=ap.parse_args();cfg=validate(json.loads(Path(args.config).read_text()))
    if not args.run_ground:print(json.dumps(cfg,indent=2));return
    from .radio_launcher import inspect_physical_interfaces
    # Reject virtual interfaces sharing one radio before changing any device.
    inspect_physical_interfaces([d['gs_interface'] for d in cfg['drones']])
    children=[]
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for d in cfg['drones']:
            children.append(subprocess.Popen(module_command('radio_launcher',
               '--role','ground','--drone',d['id'],'--config',args.config,'--binary-dir',args.binary_dir,
               '--keys',str(Path(args.keys)/d['id']),'--interface',d['gs_interface'],
               '--air-api',f'http://127.0.0.1:{d["air_api_port"]}'),env=child_env()))
        while all(c.poll() is None for c in children):time.sleep(.5)
        raise RuntimeError('One FDMA receiver stopped')
    except KeyboardInterrupt:pass
    finally:
        for c in children:
            if c.poll() is None:c.terminate()
        for c in children:
            try:c.wait(timeout=5)
            except subprocess.TimeoutExpired:c.kill();c.wait()
if __name__=='__main__':main()
