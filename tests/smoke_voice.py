"""Real cloud connectivity fixture; never a publishable production project."""
import asyncio
from unittest.mock import patch

from local_studio.common import OUTPUT,settings,setup_logging
from local_studio.voice import narrate_edge

setup_logging()
out=OUTPUT/'validation/voice-network'
out.mkdir(parents=True,exist_ok=True)
cfg={**settings(),'minimum_seconds':2,'maximum_seconds':60}
script={'chapters':[{'narration':'This is a narration connection test for The Fame Files. We are checking clear speech, real audio, and correctly timed captions before producing a full video.'}]}
with patch('local_studio.voice.settings',return_value=cfg):
    timing=asyncio.run(narrate_edge(script,out))
assert timing['cues'] and (out/'narration.wav').stat().st_size>1000
print('Actual cloud voice and caption check passed:',timing['duration'],'seconds. Fixture only; not published.')
