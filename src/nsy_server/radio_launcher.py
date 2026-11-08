#!/usr/bin/env python3
"""Prepare a dedicated compatible radio and launch one WFB-ng FDMA endpoint."""
import argparse,json,os,re,signal,subprocess,sys
from pathlib import Path
from .paths import DEFAULT_CONFIG,module_command,child_env
from .fdma import validate

def is_usb_interface(interface):
    return any(part.startswith('usb') for part in Path('/sys/class/net',interface,'device').resolve().parts)

def inspect_interface(interface):
    if (interface=='wlan0' and not is_usb_interface(interface)) or interface.startswith('REPLACE_'):
        raise ValueError('Select a dedicated external radio, never management wlan0')
    if not re.fullmatch('[A-Za-z0-9_.-]+',interface):raise ValueError('Invalid interface')
    addresses=json.loads(subprocess.check_output(['ip','-j','address','show','dev',interface],text=True))
    if any(a.get('scope')=='global' for dev in addresses for a in dev.get('addr_info',[])):
        raise ValueError('Interface has a network address; SSH/management interfaces must not be reconfigured')
    driver=Path('/sys/class/net',interface,'device/driver').resolve().name
    if not any(s in driver for s in ('8812au','88XXau','88xxau','8812eu','88x2eu')):
        raise ValueError('Unverified WFB driver: '+driver)
    info=subprocess.check_output(['iw','dev',interface,'info'],text=True)
    m=re.search(r'^\s*type (\S+)',info,re.M)
    if not m:raise ValueError('Cannot identify interface type')
    return m[1]

def inspect_physical_interfaces(interfaces,sysfs_root='/sys/class/net'):
    seen=set()
    for interface in interfaces:
        inspect_interface(interface)
        try:phy=Path(sysfs_root,interface,'phy80211').resolve(strict=True)
        except OSError as exc:raise ValueError('Cannot identify physical radio: '+interface) from exc
        if phy in seen:raise ValueError('FDMA requires independent physical radios: '+interface)
        seen.add(phy)
    return seen

def ground_receiver_command(drone):
    return module_command('rtp_receiver','--port',drone['gs_video_port'],'--headless','--http-port',drone['gs_status_port'],'--station-mode','radio','--station-name',drone['id'])

def ground_feedback_command(drone,receiver_status=None):
    status=receiver_status or ('http://127.0.0.1:'+str(drone['gs_status_port'])+'/status')
    command=module_command('feedback','--raw','--udp-port',drone['gs_feedback_port'],'--receiver-status',status)
    if receiver_status is None:command+=['--local-status','http://127.0.0.1:'+str(drone['gs_status_port'])+'/link']
    return command

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--role',choices=['ground'],default='ground')
    ap.add_argument('--drone',default='drone1');ap.add_argument('--config',default=str(DEFAULT_CONFIG))
    ap.add_argument('--interface',required=True);ap.add_argument('--binary-dir',required=True);ap.add_argument('--keys',required=True)
    ap.add_argument('--receiver-status',help='override localhost ground receiver status URL')
    ap.add_argument('--air-api',default='http://127.0.0.1:18090')
    args=ap.parse_args();cfg=validate(json.loads(Path(args.config).read_text()))
    d=next((d for d in cfg['drones'] if d['id']==args.drone),None)
    if d is None:ap.error('Unknown drone')
    previous=inspect_interface(args.interface)
    if os.geteuid()!=0:ap.error('Raw radio setup requires root: use sudo after checking the selected interface')
    key=Path(args.keys)/'gs.key'
    if not key.is_file():ap.error('Missing matching WFB keypair: '+str(key))
    binary=Path(args.binary_dir)/'wfb_rx'
    if not binary.is_file():ap.error('Missing WFB binary')
    children=[]
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        subprocess.run(['ip','link','set',args.interface,'down'],check=True)
        subprocess.run(['iw','dev',args.interface,'set','type','monitor'],check=True)
        subprocess.run(['ip','link','set',args.interface,'up'],check=True)
        subprocess.run(['iw','dev',args.interface,'set','freq',str(d['frequency_mhz']),'HT20'],check=True)
        common=[str(binary),'-R','4194304','-s','4194304','-K',str(key),'-i',str(d['link_id']),'-p','0']
        cmd=common+['-c','127.0.0.1','-u',str(d['gs_video_port']),'-T','200',args.interface]
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE if args.role=='ground' else None);children.append(proc)
        if not args.receiver_status:children.append(subprocess.Popen(ground_receiver_command(d),env=child_env()))
        reverse=Path(args.binary_dir)/'wfb_tx'
        children.append(subprocess.Popen([str(reverse),'-K',str(key),'-i',str(d['link_id']),'-p','1',
            '-u',str(d['gs_feedback_port']),'-C',str(d['gs_control_port']),'-M','0','-B','20','-S','0','-L','0','-k','1','-n','3','-T','5',args.interface]))
        helper=subprocess.Popen(ground_feedback_command(d,args.receiver_status),stdin=proc.stdout,env=child_env());children.append(helper);proc.stdout.close()
        while all(c.poll() is None for c in children):
            import time;time.sleep(.3)
        raise RuntimeError('WFB endpoint exited')
    except KeyboardInterrupt:pass
    finally:
        for c in children:
            if c.poll() is None:c.terminate()
        for c in children:
            try:c.wait(timeout=5)
            except subprocess.TimeoutExpired:c.kill();c.wait()
        subprocess.run(['ip','link','set',args.interface,'down'])
        subprocess.run(['iw','dev',args.interface,'set','type',previous])
        subprocess.run(['ip','link','set',args.interface,'up'])
if __name__=='__main__':main()
