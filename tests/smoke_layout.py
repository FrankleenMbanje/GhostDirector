"""Render the last frames of a 13-second owned fixture, beyond the old 12s ceiling."""
import json
import sys
import tempfile
from pathlib import Path
from PIL import Image
from local_studio.common import PACKAGE,browser_executable
from local_studio.motion_cli import run_motion

with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    Image.new('RGB',(800,1200),'#bca57f').save(root/'owned-fixture.png')
    props=root/'props.json'
    props.write_text(json.dumps({'photo':'owned-fixture.png','secondPhoto':'owned-fixture.png','layout':0,'label':'Renderer boundary fixture','archive':'','seconds':13}),encoding='utf-8')
    result=root/'boundary.mp4'
    run_motion(['node',str(PACKAGE/'motion/node_modules/@remotion/cli/remotion-cli.js'),'render',str(PACKAGE/'motion/src/index.ts'),'PhotoPlate',str(result),'--props',str(props),'--public-dir',str(root),'--frames=387-389','--concurrency=1','--gl='+('swangle' if sys.platform.startswith('linux') else 'angle'),'--browser-executable='+browser_executable()],cwd=PACKAGE/'motion')
    if result.stat().st_size<1000:raise RuntimeError('Photo boundary render is empty.')
print('Photo composition rendered successfully beyond twelve seconds.')
