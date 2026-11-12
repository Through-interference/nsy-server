#!/usr/bin/env python3
"""Ground-station RTP player: H.264 PT96 and JPEG PT26; reconfigures on codec switch."""
import argparse,json,socket,time,signal,threading
from http.server import ThreadingHTTPServer
from .runtime_stats import RuntimeStats
from .link_core import Metric
from dataclasses import asdict
from .viewer import Handler as PreviewHandler,LATEST_FRAME_SCRIPT

class ReceiverLink:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.lock=threading.Lock();self.metric=None;self.updated=None
    def update(self,data):
        metric=Metric(**data).validate()
        with self.lock:self.metric=asdict(metric);self.updated=self.clock()
    def status(self):
        with self.lock:
            age=self.clock()-self.updated if self.updated is not None else None
            state='unavailable' if age is None else ('stale' if age>3 else ('receiving' if self.metric['active'] else 'no-packets'))
            return dict(link=dict(self.metric) if self.metric else None,link_age_s=round(age,2) if age is not None else None,link_state=state)

class DecoderRecovery:
    """Fall back once from hardware; restart failed software pipelines with backoff."""
    def __init__(self,mode,clock=time.monotonic):
        self.mode=mode;self.clock=clock;self.disabled=False;self.restarts=0;self.retry_at=0
    def handle(self,kind,error,pipeline,null_state):
        if kind=='hardware':
            if self.mode!='auto' or self.disabled:return False
            self.disabled=True;event='hardware_fallback'
        elif kind in ('software','jpeg') and self.mode!='hardware' and pipeline is not None:
            self.restarts+=1;event='decoder_restart'
        else:return False
        if pipeline:pipeline.set_state(null_state)
        self.retry_at=self.clock()+.5
        print(json.dumps(dict(event=event,error=str(error))),flush=True)
        return True

def main():
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bind',default='127.0.0.1');ap.add_argument('--port',type=int,default=5600)
    ap.add_argument('--headless',action='store_true');ap.add_argument('--seconds',type=float,default=0)
    ap.add_argument('--http-port',type=int,help='localhost browser view after WFB/FEC and RTP decode')
    ap.add_argument('--decoder',choices=('auto','hardware','software'),default='auto')
    ap.add_argument('--station-mode',choices=('radio','udp','demo','emulation','ip'),default='udp')
    ap.add_argument('--station-name',default='drone1')
    args=ap.parse_args()
    import gi
    gi.require_version('Gst','1.0')
    from gi.repository import Gst
    Gst.init(None)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024*1024);sock.bind((args.bind,args.port));sock.settimeout(.25)
    pipeline=source=None;pt=None;last_switch=0;candidate=None;candidate_count=0;decoded=0;codecs=[]
    class View:
        def __init__(self):self.condition=threading.Condition();self.closing=False;self.frame=None;self.frames=0;self.last_frame=None
        def status(self):
            current=source
            return dict(frames=self.frames,codec=codecs[-1] if codecs else None,frame_age_s=round(time.monotonic()-self.last_frame,2) if self.last_frame else None,rtp_queue_buffers=current.get_property('current-level-buffers') if current else 0,rtp_queue_bytes=current.get_property('current-level-bytes') if current else 0)
    stats=RuntimeStats();decoder_kind=None;recovery=DecoderRecovery(args.decoder)
    original_status=View.status
    def status(self):
        result=original_status(self)
        current_source,current_pipeline=source,pipeline
        result.update(stats.sample(current_source.get_property('current-level-time')/1e6 if current_source else 0))
        if current_pipeline:
            jitter=current_pipeline.get_by_name('jitter')
            if jitter:
                j=jitter.get_property('stats')
                result['rtp_late_packets']=j.get_value('num-late');result['rtp_lost_packets']=j.get_value('num-lost')
        result.update(link.status())
        result.update(station_mode=args.station_mode,station_name=args.station_name,video_state='waiting' if self.last_frame is None else ('stale' if time.monotonic()-self.last_frame>1 else 'receiving'))
        result['decoder']=decoder_kind
        result['hardware_fallback']=recovery.disabled
        result['decoder_restarts']=recovery.restarts
        return result
    View.status=status
    view=View();web=None;link=ReceiverLink()
    if args.http_port:
        class Handler(PreviewHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.path!='/link':return self.reply(404,'Not found','text/plain')
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=8192:raise ValueError('Invalid size')
                    data=json.loads(self.rfile.read(size))
                    if not isinstance(data,dict):raise ValueError('Expected JSON object')
                    link.update(data)
                except (ValueError,TypeError) as exc:return self.reply(400,str(exc),'text/plain')
                return self.reply(200,'{"ok":true}','application/json')
            def do_GET(self):
                if self.path.split('?',1)[0]=='/':return self.reply(200,'<!doctype html><html lang="ru"><meta charset="utf-8"><title>Видео после FEC</title><style>body{background:#083e35;color:white;font:20px system-ui;margin:30px}img{width:100%;max-width:1200px}</style><h1>НСУ · видеоприёмник</h1><p id="source">Ожидание данных</p><p id="radio"></p><img id="video" alt="Ожидание свежего кадра"><p id="freshness"></p><p id="performance"></p><script>async function performanceStatus(){try{const s=await(await fetch("/status")).json();document.getElementById("source").textContent=({radio:"РАДИО · WFB-ng",ip:"WFB ПО IP · обычная Wi-Fi сеть",udp:"ПРЯМОЙ RTP · без WFB",demo:"ЛОКАЛЬНЫЙ ТЕСТ · без камеры и радио",emulation:"ЭМУЛЯЦИЯ · искусственные потери"})[s.station_mode]+" · "+s.station_name+" · "+({waiting:"ожидание видео",stale:"нет свежего кадра",receiving:"идёт приём"})[s.video_state];const l=s.link;document.getElementById("radio").textContent=l?"RSSI: "+(l.rssi??"—")+" dBm · SNR: "+(l.snr??"—")+" dB · потери после FEC: "+(100*l.post_loss).toFixed(1)+"% · метрики: "+s.link_state:"Метрик радиоканала пока нет";document.getElementById("performance").textContent="Поток: "+s.decoder_fps+" кадр/с · просмотр: "+s.preview_fps+" кадр/с · загрузка CPU: "+s.receiver_cpu_percent+"% · "+(s.receiver_overloaded?"Снижаем нагрузку":"Приёмник успевает")}catch(e){}}setInterval(performanceStatus,1000);performanceStatus()</script>'+LATEST_FRAME_SCRIPT,'text/html; charset=utf-8')
                return super().do_GET()
        web=ThreadingHTTPServer(('127.0.0.1',args.http_port),Handler);web.daemon_threads=True;web.camera=view
        threading.Thread(target=web.serve_forever,daemon=True).start()
    def count_frame(sink):
        nonlocal decoded
        sample=sink.emit('pull-sample')
        if sample is None:return Gst.FlowReturn.EOS
        decoded+=1
        stats.mark('rendered')
        if args.http_port:
            buffer=sample.get_buffer();frame=buffer.extract_dup(0,buffer.get_size())
            with view.condition:
                view.frame=frame;view.frames+=1;view.last_frame=time.monotonic();view.condition.notify_all()
        if decoded%100==0:print(json.dumps(dict(event='decoded_frames',count=decoded)),flush=True)
        return Gst.FlowReturn.OK
    def decoded_probe(pad,info):
        stats.mark('decoded');return Gst.PadProbeReturn.OK
    def handle_failure(error):
        nonlocal pipeline,source,pt,candidate,candidate_count
        if not recovery.handle(decoder_kind,error,pipeline,Gst.State.NULL):raise RuntimeError(str(error))
        pipeline=source=None;pt=candidate=None;candidate_count=0
    def check_pipeline(result=None):
        if not pipeline:return False
        message=pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message:
            if message.type==Gst.MessageType.EOS:handle_failure('Decoder reached end of stream')
            else:
                error,debug=message.parse_error();handle_failure(str(error)+' '+str(debug))
            return True
        if result is not None and result!=Gst.FlowReturn.OK:
            handle_failure('Decoder rejected packet: '+str(result));return True
        return False
    start=time.monotonic()
    try:
        while not args.seconds or time.monotonic()-start<args.seconds:
            try:data,_=sock.recvfrom(65535)
            except socket.timeout:
                check_pipeline();continue
            if time.monotonic()<recovery.retry_at:continue
            if len(data)<12 or data[0]>>6!=2:continue
            incoming=data[1]&127
            if incoming not in (26,96):continue
            if data[1]&128:stats.mark('incoming')
            if incoming!=pt:
                if candidate==incoming:candidate_count+=1
                else:candidate=incoming;candidate_count=1
                if pt is not None and (candidate_count<3 or time.monotonic()-last_switch<1):continue
                if pipeline:pipeline.set_state(Gst.State.NULL)
                codec='H264' if incoming==96 else 'JPEG'
                hardware=incoming==96 and not recovery.disabled and args.decoder!='software' and Gst.ElementFactory.find('v4l2h264dec') is not None
                if args.decoder=='hardware' and incoming==96 and not hardware:raise RuntimeError('Hardware decoder unavailable')
                decoder_kind='hardware' if hardware else ('software' if incoming==96 else 'jpeg')
                element='v4l2h264dec name=video_decoder discard-corrupted-frames=true' if hardware else 'avdec_h264 name=video_decoder max-threads=2 output-corrupt=false discard-corrupted-frames=true'
                decode='rtph264depay wait-for-keyframe=true request-keyframe=true ! video/x-h264,alignment=au ! h264parse ! '+element if incoming==96 else 'rtpjpegdepay ! jpegdec name=video_decoder'
                rate='' # Preserve every available decoded frame; no FPS limiter.
                sink=('queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '+rate+'videoscale ! video/x-raw,width=640,height=360 ! jpegenc quality=65 ! ' if args.http_port else '')+'appsink name=frames emit-signals=true sync=false max-buffers=1 drop=true' if (args.headless or args.http_port) else 'autovideosink sync=false'
                try:
                    pipeline=Gst.parse_launch(f'appsrc name=source is-live=true format=time do-timestamp=true block=false max-bytes=524288 max-buffers=256 max-time=100000000 leaky-type=downstream ! application/x-rtp,media=video,clock-rate=90000,encoding-name={codec},payload={incoming} ! rtpjitterbuffer name=jitter latency=60 drop-on-latency=true do-lost=true ! {decode} ! videoconvert ! {sink}')
                    if args.headless or args.http_port:pipeline.get_by_name('frames').connect('new-sample',count_frame)
                    pipeline.get_by_name('video_decoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,decoded_probe)
                    source=pipeline.get_by_name('source');pipeline.set_state(Gst.State.PLAYING)
                    pt=incoming;last_switch=time.monotonic();codecs.append(codec)
                    print(json.dumps(dict(event='codec',codec=codec)),flush=True)
                except Exception as error:
                    handle_failure(error);continue
            buffer=Gst.Buffer.new_allocate(None,len(data),None);buffer.fill(0,data)
            result=source.emit('push-buffer',buffer)
            check_pipeline(result)
    except KeyboardInterrupt:pass
    finally:
        if pipeline:pipeline.set_state(Gst.State.NULL)
        sock.close()
        view.closing=True
        with view.condition:view.condition.notify_all()
        if web:web.shutdown();web.server_close()
        print(json.dumps(dict(decoded_frames=decoded,codecs=codecs)),flush=True)
if __name__=='__main__':main()
