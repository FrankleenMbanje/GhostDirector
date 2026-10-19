"""Small localhost dashboard; production itself does not depend on an open browser."""
from __future__ import annotations

import html
import json
import secrets
import threading
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from .common import OUTPUT, now, settings


def serve():
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route=urlparse(self.path).path
            if route=='/status':
                path=OUTPUT/'status.json'
                value=json.loads(path.read_text()) if path.exists() else {'status':'idle','stage':'Waiting','message':'Daily local production starts at 14:00.'}
                data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if route.startswith('/media/'):
                target=(OUTPUT/unquote(route[len('/media/'):])).resolve()
                if not target.is_relative_to(OUTPUT.resolve()) or not target.is_file():
                    self.send_error(404);return
                types={'.png':'image/png','.jpg':'image/jpeg','.mp4':'video/mp4','.txt':'text/plain','.json':'application/json','.srt':'text/plain'}
                if target.suffix not in types:
                    self.send_error(403);return
                size=target.stat().st_size;start=0;end=size-1
                requested=self.headers.get('Range')
                if requested:
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested)
                    if not match:self.send_error(416);return
                    start=int(match[1]);end=min(size-1,int(match[2])) if match[2] else size-1
                    if not 0<=start<=end<size:self.send_error(416);return
                self.send_response(206 if requested else 200);self.send_header('Content-Type',types[target.suffix]);self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
                if requested:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                with target.open('rb') as source:
                    source.seek(start);remaining=end-start+1
                    while remaining and (block:=source.read(min(1024*1024,remaining))):
                        try:self.wfile.write(block)
                        except (BrokenPipeError,ConnectionResetError):break
                        remaining-=len(block)
                return
            if route!='/':self.send_error(404);return
            state_file=OUTPUT/'status.json'
            status=json.loads(state_file.read_text()) if state_file.exists() else {'stage':'Waiting','message':'Daily local production starts at 14:00.','status':'idle'}
            projects=[]
            for receipt in sorted((OUTPUT/'runs').glob('*/complete.json'),reverse=True)[:14]:
                item=json.loads(receipt.read_text());video=Path(item['video'])
                if not video.exists():continue
                url='/media/'+quote(video.relative_to(OUTPUT).as_posix())
                images=''.join('<img width="220" src="/media/'+quote(Path(p).relative_to(OUTPUT).as_posix())+'">' for p in item.get('thumbnails',[]) if Path(p).exists())
                projects.append(f'<section><h2>{html.escape(item["title"])}</h2><video controls preload="metadata" width="800" src="{url}"></video><div>{images}</div><p>Local review · {item["duration_seconds"]/60:.1f} minutes · not uploaded</p></section>')
            body=f'''<!doctype html><html><head><meta charset="utf-8"><title>The Fame Files — Local Studio</title><style>body{{background:#0d1520;color:#eee9df;font:18px Arial;max-width:1080px;margin:55px auto;padding:0 25px}}h1{{font-size:42px}}p{{line-height:1.6;color:#b7c2cc}}section{{margin:35px 0;padding:28px;background:#182431;border-radius:16px}}button{{border:0;border-radius:8px;background:#d5b383;color:#0d1520;padding:15px 24px;font-size:18px;cursor:pointer}}video{{max-width:100%}}img{{margin:16px 12px 0 0;max-width:30%}}small{{color:#d5b383}}</style></head><body data-status="{html.escape(status.get('status','idle'))}"><small>THE FAME FILES</small><h1>Your local studio</h1><p>Daily start: 2pm · South Africa time<br>Long form: at least five minutes · Uploads off</p><section><h2 id='stage'>{html.escape(status['stage'])}</h2><p id='message'>{html.escape(status['message'])}</p><form method="post" action="/run"><input type="hidden" name="token" value="{token}"><button>Run now</button></form></section>{''.join(projects) or '<p>Completed videos and three thumbnail options will appear here. If source quality is insufficient, the run stops with a clear report.</p>'}<p>Genuine celebrity media and original graphics · No stock footage</p><script>
let previous=document.body.dataset.status;
setInterval(async()=>{{try{{const response=await fetch('/status');const data=await response.json();document.getElementById('stage').innerText=data.stage;document.getElementById('message').innerText=data.message;const playing=Array.from(document.querySelectorAll('video')).some(v=>!v.paused&&!v.ended);if(data.status==='complete'&&previous!=='complete'&&!playing)location.reload();previous=data.status;}}catch(e){{}}}},30000);
</script></body></html>'''
            encoded=body.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'))
            if length>4096:self.send_error(413);return
            form=parse_qs(self.rfile.read(length).decode())
            if self.path!='/run' or form.get('token')!=[token]:self.send_error(403);return
            from .__main__ import daily
            threading.Thread(target=daily,daemon=True).start()
            self.send_response(303);self.send_header('Location','/');self.end_headers()
        def log_message(self,*args):pass
    from pathlib import Path
    server=ThreadingHTTPServer(('127.0.0.1',9042),Handler)
    print('Local Studio: http://127.0.0.1:9042',flush=True)
    server.serve_forever()
