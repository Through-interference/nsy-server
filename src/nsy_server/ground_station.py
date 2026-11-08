"""Laptop NSU: WFB radio reception, direct RTP, or an explicit local video demo."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import math
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from .fdma import validate
from .radio_launcher import inspect_interface
from .paths import DEFAULT_CONFIG,PROJECT_ROOT,module_command,child_env


def parser():
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group()
    modes.add_argument('--mode',choices=('radio','udp','demo','ip'))
    modes.add_argument('--demo',dest='mode',action='store_const',const='demo')
    ap.set_defaults(mode='radio')
    ap.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    ap.add_argument('--drone',default='drone1')
    ap.add_argument('--interface',help='dedicated external Linux monitor/injection interface')
    ap.add_argument('--keys',type=Path,help='directory containing the matching gs.key')
    ap.add_argument('--binary-dir',type=Path,default=PROJECT_ROOT/'vendor/skvoz-wfb-ng')
    ap.add_argument('--peer',help='Pi IP for the client-initiated WFB/IP carrier')
    ap.add_argument('--network-port',type=int,default=17030)
    ap.add_argument('--bind',default='127.0.0.1',help='RTP bind address in udp mode; HTTP remains localhost')
    ap.add_argument('--video-port',type=int,help='override RTP port in udp/demo modes')
    ap.add_argument('--http-port',type=int,help='override viewer port in udp/demo modes')
    ap.add_argument('--demo-codec',choices=('h264','jpeg'),default='h264')
    ap.add_argument('--fps',type=int,default=30,help='synthetic source FPS, 1..120')
    ap.add_argument('--seconds',type=float,default=0,help='stop after a bounded run; 0 means until Ctrl+C')
    ap.add_argument('--log-dir',type=Path,default=PROJECT_ROOT/'results/nsu')
    actions=ap.add_mutually_exclusive_group()
    actions.add_argument('--check',action='store_true',help='read-only readiness report')
    actions.add_argument('--plan',action='store_true',help='print process commands without starting them')
    return ap


def load_drone(args):
    cfg=validate(json.loads(args.config.read_text()))
    drone=next((dict(d) for d in cfg['drones'] if d['id']==args.drone),None)
    if drone is None:raise ValueError('Unknown drone: '+args.drone)
    if args.mode=='radio' and (args.video_port is not None or args.http_port is not None):
        raise ValueError('In radio mode set ports in config/fdma.json so all processes agree')
    for value,key in ((args.video_port,'gs_video_port'),(args.http_port,'gs_status_port')):
        if value is not None:
            if not 1024<=value<=65535:raise ValueError('Ports must be in 1024..65535')
            drone[key]=value
    if not 1<=args.fps<=120 or not math.isfinite(args.seconds) or args.seconds<0:raise ValueError('Invalid FPS or duration')
    if args.mode not in ('udp','ip') and args.bind!='127.0.0.1':raise ValueError('Only UDP mode accepts an external bind address')
    if args.mode=='ip':
        if not args.peer:raise ValueError('IP mode requires --peer PI_IP')
        if not 1024<=args.network_port<=65535 or max(drone['gs_video_port'],drone['gs_feedback_port'])>55535:raise ValueError('Invalid IP carrier ports')
    if args.interface:drone['gs_interface']=args.interface
    return drone


def build_commands(args,drone):
    if args.mode=='radio':
        keys=args.keys or PROJECT_ROOT/'keys'/args.drone
        return [('radio',module_command('radio_launcher','--role','ground','--drone',args.drone,
                    '--config',args.config.resolve(),'--interface',drone['gs_interface'],
                    '--binary-dir',args.binary_dir.resolve(),'--keys',keys.resolve()))]
    commands=[('receiver',module_command('rtp_receiver','--port',drone['gs_video_port'],
        '--bind',args.bind if args.mode!='ip' else '127.0.0.1','--headless','--http-port',drone['gs_status_port'],
        '--station-mode',args.mode,'--station-name',args.drone))]
    if args.mode=='ip':
        keys=args.keys or PROJECT_ROOT/'keys'/args.drone;key=str(keys.resolve()/'gs.key');binary=args.binary_dir.resolve()
        incoming=drone['gs_video_port']+10000;outgoing=drone['gs_feedback_port']+10000
        commands += [
            ('wfb-rx',[str(binary/'wfb_rx'),'-a',str(incoming),'-u',str(drone['gs_video_port']),'-K',key,'-i',str(drone['link_id']),'-p','0','-T','200','-R','4194304','-s','4194304']),
            ('wfb-tx',[str(binary/'wfb_tx'),'-D',str(outgoing),'-u',str(drone['gs_feedback_port']),'-C',str(drone['gs_control_port']),'-K',key,'-i',str(drone['link_id']),'-p','1','-M','0','-k','1','-n','3','-T','5','ip-carrier']),
            ('feedback',module_command('feedback','--raw','--source','wfb-ip','--udp-port',drone['gs_feedback_port'],'--receiver-status','http://127.0.0.1:'+str(drone['gs_status_port'])+'/status','--local-status','http://127.0.0.1:'+str(drone['gs_status_port'])+'/link')),
            ('carrier',module_command('udp_link','--role','client','--bind',args.bind,'--peer',args.peer,'--port',args.network_port,'--local-input',outgoing,'--local-output',incoming,'--status',args.log_dir/'carrier.json'))]
    if args.mode=='demo':
        cmd=['gst-launch-1.0','-q','videotestsrc','is-live=true','pattern=ball','!',
             f'video/x-raw,format=I420,width=640,height=480,framerate={args.fps}/1','!']
        if args.demo_codec=='h264':
            cmd+=['x264enc','tune=zerolatency','speed-preset=ultrafast','bitrate=2500',
                  'key-int-max='+str(args.fps),'byte-stream=true','!',
                  'rtph264pay','pt=96','config-interval=-1','mtu=1200']
        else:cmd+=['jpegenc','quality=65','!','rtpjpegpay','pt=26','mtu=1200']
        cmd+=['!','udpsink','host=127.0.0.1','port='+str(drone['gs_video_port']),'sync=false']
        commands.append(('demo',cmd))
    return commands


def available_radios():
    result=[]
    for interface in sorted(Path('/sys/class/net').iterdir()):
        if not (interface/'phy80211').exists():continue
        result.append(dict(interface=interface.name,driver=(interface/'device/driver').resolve().name,
                           phy=(interface/'phy80211').resolve().name))
    return result


def runtime_diagnostics(mode='udp',codec='h264'):
    required=['appsrc','rtpjitterbuffer','rtph264depay','h264parse','avdec_h264',
              'rtpjpegdepay','jpegdec','videoconvert','videoscale','jpegenc','appsink']
    if mode=='demo':required+=['videotestsrc','udpsink','rtph264pay','x264enc'] if codec=='h264' else ['videotestsrc','udpsink','rtpjpegpay']
    probe="""import gi,json,sys
try:
 gi.require_version('Gst','1.0')
 from gi.repository import Gst
 Gst.init(None)
 missing=[name for name in json.loads(sys.argv[1]) if Gst.ElementFactory.find(name) is None]
 print(json.dumps(dict(ready=not missing,missing=missing,version=Gst.version_string())))
except Exception as exc:print(json.dumps(dict(ready=False,missing=['GStreamer/Python GI'],error=str(exc))))
"""
    try:
        result=subprocess.run([sys.executable,'-c',probe,json.dumps(required)],env=child_env(),capture_output=True,text=True,timeout=15)
        return json.loads(result.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:return dict(ready=False,missing=['GStreamer probe'],error=str(exc))


def readiness(args,drone):
    runtime=runtime_diagnostics(args.mode,args.demo_codec);radios=available_radios();issues=[]
    if not runtime['ready']:issues.append('Missing decoder/runtime: '+', '.join(runtime['missing']))
    if args.mode=='demo' and not shutil.which('gst-launch-1.0'):issues.append('Missing gst-launch-1.0')
    if args.mode=='radio':
        selected=drone['gs_interface']
        if selected not in [r['interface'] for r in radios]:issues.append('Physical Wi-Fi interface unavailable: '+selected)
        for command in ('iw','ip'):
            if not shutil.which(command):issues.append('Missing tool: '+command)
        if selected in [r['interface'] for r in radios] and shutil.which('iw') and shutil.which('ip'):
            try:inspect_interface(selected)
            except (OSError,ValueError,subprocess.SubprocessError) as exc:issues.append('Radio interface rejected: '+str(exc))
    if args.mode in ('radio','ip'):
        for name in ('wfb_rx','wfb_tx'):
            if not os.access(args.binary_dir/name,os.X_OK):issues.append('Missing WFB binary: '+str(args.binary_dir/name))
        key=(args.keys or PROJECT_ROOT/'keys'/args.drone)/'gs.key'
        if not key.is_file() or not key.stat().st_size:issues.append('Matching gs.key required: '+str(key))
    return dict(ready=not issues,mode=args.mode,drone=args.drone,wsl='microsoft' in platform.release().lower(),
                interfaces=radios,runtime=runtime,issues=issues,requires_root=args.mode=='radio',
                hints=['WSL needs a USB/IP-attached adapter and Linux WFB driver; Windows eth aliases are not monitor radios'] if 'microsoft' in platform.release().lower() and not radios else [],
                video_port=drone['gs_video_port'],viewer='http://127.0.0.1:'+str(drone['gs_status_port']))


def port_issues(args,drone):
    endpoints=[('HTTP','127.0.0.1',drone['gs_status_port'],socket.SOCK_STREAM),
               ('RTP',args.bind,drone['gs_video_port'],socket.SOCK_DGRAM)]
    if args.mode in ('radio','ip'):endpoints += [('feedback','127.0.0.1',drone['gs_feedback_port'],socket.SOCK_DGRAM),('control','127.0.0.1',drone['gs_control_port'],socket.SOCK_DGRAM)]
    if args.mode=='ip':endpoints += [('WFB RX','127.0.0.1',drone['gs_video_port']+10000,socket.SOCK_DGRAM),('carrier','127.0.0.1',drone['gs_feedback_port']+10000,socket.SOCK_DGRAM),('network bind',args.bind,0,socket.SOCK_DGRAM)]
    issues=[]
    for name,host,port,kind in endpoints:
        try:
            with socket.socket(socket.AF_INET,kind) as sock:
                # Match ThreadingHTTPServer.allow_reuse_address after a recent close.
                if kind == socket.SOCK_STREAM:sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                sock.bind((host,port))
        except OSError as exc:issues.append(f'{name} {host}:{port} unavailable: {exc}')
    return issues

def stop_children(children):
    for proc in reversed(children):
        if proc.poll() is None:
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    for proc in reversed(children):
        try:proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.wait(timeout=3)


def main():
    ap=parser();args=ap.parse_args()
    try:drone=load_drone(args)
    except (OSError,ValueError,KeyError) as exc:ap.error(str(exc))
    if args.plan:
        print(json.dumps(dict(mode=args.mode,drone=drone,processes=build_commands(args,drone)),indent=2));return
    report=readiness(args,drone)
    if report['ready']:
        report['issues'].extend(port_issues(args,drone));report['ready']=not report['issues']
    report['isolated_runtime']=os.environ.get('SKVOZ_NSU_RUNTIME')
    if args.check:print(json.dumps(report,indent=2));raise SystemExit(0 if report['ready'] else 1)
    if not report['ready']:
        print(json.dumps(report,indent=2),file=sys.stderr)
        ap.exit(1,'НСУ не готова: см. issues. Установка: bash scripts/install-nsu.sh\n')
    if args.mode=='radio' and os.geteuid()!=0:ap.error('Radio setup needs sudo; --check and udp/demo do not')
    args.log_dir.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();children=[];logs=[]
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}));start=time.monotonic()
    rx_pipe=None
    try:
        for name,command in build_commands(args,drone):
            log=(args.log_dir/(name+'.log')).open('a');logs.append(log)
            stdin=rx_pipe if name=='feedback' and args.mode=='ip' else None
            proc=subprocess.Popen(command,env=child_env(),stdin=stdin,stdout=subprocess.PIPE if name=='wfb-rx' else log,stderr=log,start_new_session=True)
            children.append(proc)
            if name=='wfb-rx':rx_pipe=proc.stdout
            if stdin is not None:stdin.close()
            if name in ('receiver','radio'):
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and not stop.is_set():
                    if children[-1].poll() is not None:raise RuntimeError('Receiver exited; see '+str(args.log_dir/(name+'.log')))
                    try:
                        with op.open(report['viewer']+'/status',timeout=.5) as response:json.load(response)
                        break
                    except (OSError,ValueError):time.sleep(.1)
                else:raise RuntimeError('Receiver HTTP startup timeout')
        print(json.dumps(dict(event='nsu_ready',mode=args.mode,viewer=report['viewer'],logs=str(args.log_dir)),ensure_ascii=False),flush=True)
        while not stop.wait(.2):
            if any(p.poll() is not None for p in children):raise RuntimeError('NSU subprocess stopped; inspect '+str(args.log_dir))
            if args.seconds and time.monotonic()-start>=args.seconds:break
    except KeyboardInterrupt:pass
    except (OSError,RuntimeError) as exc:ap.exit(1,str(exc)+'\n')
    finally:
        stop_children(children)
        for log in logs:log.close()

if __name__=='__main__':main()
