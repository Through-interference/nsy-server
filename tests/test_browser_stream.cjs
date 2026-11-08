const fs=require('fs'),vm=require('vm'),assert=require('assert');
// Rendered LATEST_FRAME_SCRIPT on stdin; command in ADAPTIVE.md.
const script=fs.readFileSync(0,'utf8');
const chunks=[];for(let i=1;i<=120;i++){const body=String(i);chunks.push(Buffer.from('--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+body.length+'\r\n\r\n'+body+'\r\n'))}
const stream=Buffer.concat(chunks);let offset=0,last=0,shown=0,alive=0,peak=0;
const context={drawImage(bitmap){assert(bitmap.id>last);last=bitmap.id;shown++}};
const canvas={style:{},getContext:()=>context,width:0,height:0};
const image={before(){},hidden:false};
vm.runInNewContext(script,{document:{addEventListener(){},hidden:false,createElement:()=>canvas,getElementById:id=>id==='video'?image:{}},performance,AbortController,Blob,Uint8Array,TextDecoder,URL,setTimeout,clearTimeout,setInterval,clearInterval,
createImageBitmap:async blob=>{await new Promise(r=>setTimeout(r,4));const id=Number(await blob.text());alive++;peak=Math.max(peak,alive);return {id,width:640,height:360,close(){alive--}}},
requestAnimationFrame:cb=>setTimeout(()=>cb(performance.now()),8),fetch:async()=>({ok:true,body:{getReader:()=>({async read(){await new Promise(r=>setTimeout(r,1));if(offset>=stream.length)return {done:true};const end=Math.min(stream.length,offset+1+(offset%503));const value=stream.subarray(offset,end);offset=end;return {done:false,value}}})}})});
setTimeout(()=>{assert.equal(last,120);assert(shown>3);assert.equal(alive,0);assert(peak<=2);console.log(JSON.stringify({frames_received:120,frames_painted:shown,last_frame:last,max_live_bitmaps:peak}));process.exit(0)},2500);
