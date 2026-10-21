"""Live cloud download check; fixture does not enter any production receipt."""
import tempfile
from pathlib import Path
from PIL import Image
from local_studio.media import candidates,fetch

pool=candidates('Charli XCX','bitmap')
if not pool:raise RuntimeError('No licensed archive fixture is available.')
with tempfile.TemporaryDirectory() as directory:
    path=fetch(pool[0],Path(directory))
    with Image.open(path) as picture:
        picture.verify()
print('Actual licensed Wikimedia photo download and image decoding passed.')
