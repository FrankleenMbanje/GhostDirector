"""Download official local model files. Never read browser cookies or API keys."""
from __future__ import annotations

import hashlib
import argparse
import shutil
import subprocess
import json
from pathlib import Path

import requests

from .common import RUNTIME, save, settings, space_check


def download(url: str, target: Path, expected_digest: str | None = None):
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + '.part')
    # Use resumable native HTTPS transfers for large weights on unreliable links.
    curl=shutil.which('curl.exe') or shutil.which('curl')
    if curl:
        subprocess.run([curl,'--fail','--location','--silent','--show-error','--continue-at','-','--retry','5','--retry-delay','2','--connect-timeout','20','--max-time','1800','--speed-limit','2048','--speed-time','30','--output',str(partial),url],check=True)
    else:
        start=partial.stat().st_size if partial.exists() else 0
        headers={'Range':f'bytes={start}-'} if start else {}
        with requests.get(url, headers=headers,stream=True,timeout=(20,60)) as response:
            response.raise_for_status()
            mode='ab' if start and response.status_code==206 else 'wb'
            with partial.open(mode) as handle:
                for chunk in response.iter_content(16*1024):
                    if chunk:handle.write(chunk)
    digest = hashlib.sha256(partial.read_bytes()).hexdigest()
    if expected_digest and expected_digest.removeprefix('sha256:') != digest:
        raise RuntimeError('Official release checksum mismatch; model not installed.')
    partial.replace(target)
    print('Installed', target.name, target.stat().st_size, flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--voice-only',action='store_true')
    parser.add_argument('--model-only',action='store_true')
    args=parser.parse_args()
    space_check()
    cfg = settings()
    if not args.voice_only:
        from .ai import ensure_server, LocalAI
        from .common import setup_logging
        setup_logging()
        ensure_server()
        LocalAI().install_if_missing()
    if args.model_only:
        print('LOCAL_AI_MODEL_READY',flush=True)
        return
    release = requests.get('https://api.github.com/repos/thewh1teagle/kokoro-onnx/releases/tags/model-files-v1.1', timeout=30)
    release.raise_for_status()
    assets = {x['name']: x for x in release.json()['assets']}
    records = []
    for name in ['kokoro-v1.0.onnx', 'voices-v1.0.bin']:
        asset = assets[name]
        target = RUNTIME / 'voice' / name
        download(asset['browser_download_url'], target, asset.get('digest'))
        records.append({'file': name, 'source': asset['browser_download_url'], 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'license': 'Kokoro-82M Apache-2.0; runtime MIT', 'license_source': 'https://huggingface.co/hexgrad/Kokoro-82M'})
    save(RUNTIME / 'model_licenses.json', records)
    print('LOCAL_MODELS_READY', flush=True)


if __name__ == '__main__':
    main()
