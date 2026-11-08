#!/usr/bin/env python3
"""Inject a GStreamer error after the real hardware decoder; dedicated localhost ports only."""
import argparse,json,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
from nsy_server.paths import PROJECT_ROOT,child_env

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--h264',required=True);ap.add_argument('--output',default='results/fixes-decoder-fault.json');args=ap.parse_args()
    source=Path(args.h264).resolve();assert source.stat().st_size>1000
    runner="""import sys,gi
    gi.require_version('Gst','1.0')
    from gi.repository import Gst
    Gst.init(None)
    if Gst.ElementFactory.find('v4l2h264dec') is None:raise RuntimeError('Hardware decoder required')
    original=Gst.parse_launch
    def faulty(spec):
        return original(spec.replace('v4l2h264dec name=video_decoder discard-corrupted-frames=true','v4l2h264dec name=video_decoder discard-corrupted-frames=true ! identity error-after=20'))
    Gst.parse_launch=faulty
    from nsy_server import rtp_receiver
    sys.argv=['rtp_receiver','--port','17700','--http-port','17701','--headless','--seconds','12']
    rtp_receiver.main()
    """
    runner='\n'.join(line[4:] if line.startswith('    ') else line for line in runner.splitlines())
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}));sender=None;result=None
    with tempfile.TemporaryDirectory() as tmp:
        with open(Path(tmp)/'receiver.log','w+') as log:
            receiver=subprocess.Popen([sys.executable,'-c',runner],cwd=PROJECT_ROOT,env=child_env(),stdout=log,stderr=log)
            try:
                time.sleep(1)
                sender=subprocess.Popen(['gst-launch-1.0','-q','filesrc','location='+str(source),'!','h264parse','!','video/x-h264,alignment=au','!','identity','sleep-time=33333','!','rtph264pay','pt=96','config-interval=-1','!','udpsink','host=127.0.0.1','port=17700','sync=true'],stdout=log,stderr=log)
                deadline=time.monotonic()+9
                while time.monotonic()<deadline:
                    assert receiver.poll() is None,'Receiver terminated during decoder recovery'
                    with op.open('http://127.0.0.1:17701/status',timeout=2) as response:state=json.load(response)
                    if state['hardware_fallback'] and state['decoder']=='software' and state['frames']>30:
                        result=dict(passed=True,status=state,scope='injected pipeline failure after actual v4l2h264dec');break
                    time.sleep(.25)
                assert result is not None,'No decoded frames after recovery'
            finally:
                for proc in (sender,receiver):
                    if proc is not None and proc.poll() is None:proc.terminate();proc.wait(timeout=5)
                log.seek(0);logs=log.read()
                if result is None:print(logs,file=sys.stderr)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2));print(json.dumps(result))
if __name__=='__main__':main()
