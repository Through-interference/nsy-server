#!/usr/bin/env python3
"""Real laptop GStreamer/RTP/HTTP check, no camera, SSH or radio interface needed."""
import argparse,json,os,signal,socket,subprocess,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def free_port(kind):
    with socket.socket(socket.AF_INET,kind) as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,default=ROOT/'results/nsu-integration.json');args=ap.parse_args()
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}));results=[]
    # No source: the laptop must still expose a waiting-state page and shut down cleanly.
    video,http=free_port(socket.SOCK_DGRAM),free_port(socket.SOCK_STREAM)
    base='http://127.0.0.1:'+str(http);idle=ROOT/'results/nsu-integration/idle';idle.mkdir(parents=True,exist_ok=True)
    with (idle/'launcher.log').open('w') as log:
        proc=subprocess.Popen(['bash',str(ROOT/'start-nsu.sh'),'--mode','udp','--video-port',str(video),'--http-port',str(http),'--seconds','15','--log-dir',str(idle)],stdout=log,stderr=log,start_new_session=True)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if proc.poll() is not None:raise RuntimeError('Idle station exited')
                try:
                    with op.open(base+'/status',timeout=1) as response:state=json.load(response)
                    break
                except OSError:time.sleep(.1)
            else:raise AssertionError('Idle viewer unavailable')
            assert state['station_mode']=='udp' and state['video_state']=='waiting' and state['frames']==0
            try:op.open(base+'/frame.jpg',timeout=1)
            except urllib.error.HTTPError as exc:assert exc.code==503
            else:raise AssertionError('Missing video must not return a fabricated frame')
            blocked=subprocess.run(['bash',str(ROOT/'start-nsu.sh'),'--mode','udp','--video-port',str(free_port(socket.SOCK_DGRAM)),'--http-port',str(http),'--check'],capture_output=True,text=True,timeout=15)
            assert blocked.returncode==1 and 'unavailable' in blocked.stdout
            results.append(dict(mode='udp',passed=True,scope='no source: waiting UI, no fabricated frame, busy port rejected'))
        finally:
            if proc.poll() is None:os.killpg(proc.pid,signal.SIGTERM)
            proc.wait(timeout=12)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:sock.bind(('127.0.0.1',video))
    for codec in ('h264','jpeg'):
        video,http=free_port(socket.SOCK_DGRAM),free_port(socket.SOCK_STREAM)
        base='http://127.0.0.1:'+str(http);logs=ROOT/'results/nsu-integration'/codec;logs.mkdir(parents=True,exist_ok=True)
        with (logs/'launcher.log').open('w') as log:
            proc=subprocess.Popen(['bash',str(ROOT/'start-nsu.sh'),'--demo','--demo-codec',codec,'--fps','30','--video-port',str(video),'--http-port',str(http),'--seconds','25','--log-dir',str(logs)],cwd='/tmp',stdout=log,stderr=log,start_new_session=True)
            try:
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if proc.poll() is not None:raise RuntimeError('NSU exited: '+str(logs/'launcher.log'))
                    try:
                        with op.open(base+'/status',timeout=1) as response:status=json.load(response)
                        if status['frames']>=15 and status['video_state']=='receiving':break
                    except OSError:pass
                    time.sleep(.2)
                else:raise AssertionError('No decoded video; inspect '+str(logs))
                assert status['station_mode']=='demo'
                assert status['codec']==('H264' if codec=='h264' else 'JPEG'),status
                with op.open(base+'/frame.jpg',timeout=2) as response:frame=response.read()
                assert frame.startswith(b'\xff\xd8') and frame.endswith(b'\xff\xd9')
                request=urllib.request.Request(base+'/link',data=json.dumps(dict(rssi=-67,snr=19,pre_loss=.05,post_loss=0,active=True,source='simulation')).encode(),headers={'Content-Type':'application/json'})
                with op.open(request,timeout=2) as response:assert response.status==200
                with op.open(base+'/status',timeout=2) as response:updated=json.load(response)
                assert updated['link']['rssi']==-67 and updated['link_state']=='receiving'
                request=urllib.request.Request(base+'/link',data=b'{"active":"false"}',headers={'Content-Type':'application/json'})
                try:op.open(request,timeout=2)
                except urllib.error.HTTPError as exc:assert exc.code==400
                else:raise AssertionError('Invalid metrics accepted')
                time.sleep(3)
                with op.open(base+'/status',timeout=2) as response:final=json.load(response)
                assert final['frames']>status['frames']
                assert final['decoder_fps']>=20,final
                results.append(dict(codec=codec,passed=True,decoded_frames=final['decoder_frames'],jpeg_frames=final['frames'],fps=final['decoder_fps'],receiver=final['decoder'],frame_bytes=len(frame),scope='synthetic localhost RTP; no RF'))
            finally:
                if proc.poll() is None:os.killpg(proc.pid,signal.SIGTERM)
                proc.wait(timeout=12)
        # A stopped station must release the receiver HTTP/UDP sockets.
        with socket.socket() as sock:
            sock.settimeout(.2)
            assert sock.connect_ex(('127.0.0.1',http))!=0,'Receiver process leaked after stop'
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:sock.bind(('127.0.0.1',video))
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(dict(passed=True,tests=results),indent=2));print(json.dumps(results))
if __name__=='__main__':main()
