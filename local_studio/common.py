from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(__file__).resolve().parent
RUNTIME = ROOT / '.local-runtime'
OUTPUT = ROOT / 'output' / 'local-studio'
LOG = logging.getLogger('local-studio')


class QualityBlocked(RuntimeError):
    """The run needs better source material, not a lower-quality fallback."""


def browser_executable() -> str:
    candidates = [os.environ.get('FAME_CHROME_EXECUTABLE', ''),
                  'C:/Program Files/Google/Chrome/Application/chrome.exe',
                  shutil.which('google-chrome') or '', shutil.which('chromium') or '']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise QualityBlocked('Install Chrome or set FAME_CHROME_EXECUTABLE for local motion rendering.')


def settings() -> dict:
    cfg = json.loads((PACKAGE / 'settings.json').read_text(encoding='utf-8'))
    if cfg.get('uploads_enabled') is not False:
        raise QualityBlocked('This local production system does not support uploads.')
    return cfg


def now() -> datetime:
    return datetime.now(ZoneInfo(settings()['timezone']))


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:90]


def command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def probe(path: Path) -> dict:
    return json.loads(command(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]).stdout)


def duration(path: Path) -> float:
    return float(probe(path)['format']['duration'])


def space_check() -> None:
    free = shutil.disk_usage(ROOT).free / 1024**3
    if free < settings()['minimum_free_gb']:
        raise QualityBlocked(f'Only {free:.1f}GB free. Free disk space before production; existing videos were preserved.')


def setup_logging() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    handlers=[logging.FileHandler(OUTPUT/'daily.log',encoding='utf-8')]
    if sys.stderr is not None:handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',handlers=handlers)
