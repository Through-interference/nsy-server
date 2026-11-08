"""Local JPEG/HTTP output used by the laptop RTP decoder."""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
import json,socket,time
PAGE="<html><p>NSU receiver</p></html>"

class Handler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,65536)
    def reply(self, code, content, content_type):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            return self.reply(200, PAGE, 'text/html; charset=utf-8')
        if path == '/frame.jpg':
            cam=self.server.camera
            after=self.headers.get('X-After-Frame')
            try:after=int(after) if after is not None else None
            except ValueError:return self.reply(400,'Invalid frame sequence','text/plain')
            with cam.condition:
                if after is not None:
                    cam.condition.wait_for(lambda:cam.closing or cam.frames!=after,timeout=1)
                    if cam.frames==after:return self.reply(204,b'','image/jpeg')
                sequence=cam.frames
                frame=cam.frame
                age=time.monotonic()-cam.last_frame if cam.last_frame else None
            if frame is None:return self.reply(503,'Waiting for camera','text/plain')
            self.send_response(200)
            self.send_header('Content-Type','image/jpeg')
            self.send_header('X-Frame-Sequence',str(sequence))
            self.send_header('Content-Length',str(len(frame)))
            self.send_header('Cache-Control','no-store, no-cache, max-age=0')
            if age is not None:self.send_header('X-Frame-Age-Ms',str(round(age*1000)))
            self.end_headers();self.wfile.write(frame)
            return
        if path == '/status':
            return self.reply(200, json.dumps(self.server.camera.status()), 'application/json')
        if path != '/stream.mjpg':
            return self.reply(404, 'Not found', 'text/plain')
        self.connection.settimeout(10)
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        seq = -1
        cam = self.server.camera
        try:
            while not cam.closing:
                with cam.condition:
                    cam.condition.wait_for(lambda: cam.closing or (cam.frame is not None and cam.frames != seq), timeout=10)
                    if cam.closing:
                        break
                    if cam.frame is None or cam.frames == seq:
                        continue
                    frame, seq = cam.frame, cam.frames
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        self.close_connection = True

LATEST_FRAME_SCRIPT = """<script>(async function showLatest(){
const image=document.getElementById('video'),label=document.getElementById('freshness');
let latest=null,previous=null,shown=0,since=performance.now(),displayFPS=0,lastReceived=0,wake=null,ready=null;
const canvas=document.createElement('canvas');canvas.style.cssText='width:100%;max-width:1200px;display:block;background:#00281f';
const useCanvas=typeof createImageBitmap==='function';const ctx=useCanvas?canvas.getContext('2d',{alpha:false,desynchronized:true}):null;
if(ctx){image.before(canvas);image.hidden=true}
let lastPaint=null,gapMax=0;
// Read continuously, but retain only the newest complete JPEG for the renderer.
// Network receive and image decoding run independently; neither creates a frame queue.
(async function receive(){for(;;){const controller=new AbortController();let watchdog=null;
try{if(document.hidden)await new Promise(resolve=>document.addEventListener('visibilitychange',resolve,{once:true}));
const onVisibility=()=>{if(document.hidden)controller.abort()};document.addEventListener('visibilitychange',onVisibility,{signal:controller.signal});
const response=await fetch('/stream.mjpg',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('Stream unavailable');
const reader=response.body.getReader();let buffer=new Uint8Array(0),length=null;lastReceived=performance.now();
watchdog=setInterval(()=>{if(performance.now()-lastReceived>5000)controller.abort()},1000);
for(;;){const part=await reader.read();if(part.done)break;lastReceived=performance.now();
const joined=new Uint8Array(buffer.length+part.value.length);joined.set(buffer);joined.set(part.value,buffer.length);buffer=joined;
for(;;){if(length===null){let end=-1;for(let i=0;i+3<buffer.length;i++){if(buffer[i]===13&&buffer[i+1]===10&&buffer[i+2]===13&&buffer[i+3]===10){end=i;break}}
if(end<0){if(buffer.length>8192)throw Error('Invalid stream header');break}
const header=new TextDecoder().decode(buffer.subarray(0,end));const match=/Content-Length:\s*(\d+)/i.exec(header);if(!match)throw Error('Missing frame length');length=Number(match[1]);if(length<1||length>4194304)throw Error('Invalid frame size');buffer=buffer.slice(end+4)}
if(buffer.length<length)break;latest=new Blob([buffer.slice(0,length)],{type:'image/jpeg'});buffer=buffer.slice(length);length=null;if(wake){const next=wake;wake=null;next()}}
}
}catch(e){}finally{if(watchdog)clearInterval(watchdog);controller.abort()}
await new Promise(resolve=>setTimeout(resolve,300))}})();
// Decode without changing the visible image. The previous canvas remains intact.
(async function decode(){for(;;){if(!latest)await new Promise(resolve=>wake=resolve);if(!latest)continue;
const frame=latest;latest=null;
try{if(ctx){const bitmap=await createImageBitmap(frame);if(ready)ready.close();ready=bitmap}
else{const url=URL.createObjectURL(frame);image.src=url;await image.decode();if(previous)URL.revokeObjectURL(previous);previous=url;shown++}}
catch(e){}
}})();
function paint(now){if(ctx&&ready){const bitmap=ready;ready=null;
if(canvas.width!==bitmap.width||canvas.height!==bitmap.height){canvas.width=bitmap.width;canvas.height=bitmap.height}
ctx.drawImage(bitmap,0,0);bitmap.close();shown++;
if(lastPaint!==null)gapMax=Math.max(gapMax,now-lastPaint);lastPaint=now}
if(now-since>=1000){displayFPS=1000*shown/(now-since);shown=0;since=now;
if(label)label.textContent='Показ в браузере: '+displayFPS.toFixed(1)+' кадр/с · наибольший интервал: '+Math.round(gapMax)+' мс · без ограничения FPS';gapMax=0}
requestAnimationFrame(paint)}requestAnimationFrame(paint);
})();</script>"""
