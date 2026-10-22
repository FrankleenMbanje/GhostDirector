from __future__ import annotations

import re
import asyncio
import socket
import hashlib
import json
import html
import wave
import subprocess
from pathlib import Path

from .common import LOG, RUNTIME, QualityBlocked, command, duration, save, settings


def phrases(text: str) -> list[str]:
    """Short natural clauses make offline captions match measured audio exactly."""
    result = []
    for sentence in re.split(r'(?<=[.!?;])\s+', text.strip()):
        words = sentence.split()
        while words:
            count = min(12, len(words))
            while count > 1 and len(' '.join(words[:count])) > 68:
                count -= 1
            if len(words) > count:
                boundaries = [i + 1 for i, word in enumerate(words[:count]) if word.endswith((',', ':')) and i >= 4]
                if boundaries:
                    count = boundaries[-1]
            result.append(' '.join(words[:count]))
            words = words[count:]
    return result


def stamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'


def wrap(text: str) -> str:
    if len(text) <= 35:
        return text
    words = text.split()
    middle = min(range(1, len(words)), key=lambda i: abs(len(' '.join(words[:i])) - len(' '.join(words[i:]))))
    return ' '.join(words[:middle]) + '\n' + ' '.join(words[middle:])


def align_source_words(words: list[dict],text: str) -> list[dict]:
    """Restore punctuation without losing timing when the service splits a hyphenated word."""
    clean=lambda value:re.sub(r'[^\w]','',value.casefold())
    result=[];cursor=0
    for original in text.split():
        wanted=clean(original)
        if not wanted:continue
        first=cursor;combined=''
        while cursor<len(words) and wanted.startswith(combined):
            combined+=clean(words[cursor]['word']);cursor+=1
            if combined==wanted:
                result.append({'start':words[first]['start'],'end':words[cursor-1]['end'],'word':original});break
        else:return words
        if combined!=wanted:return words
    return result if cursor==len(words) else words


def merge_orphan_cues(cues):
    result=[]
    for cue in cues:
        previous=result[-1] if result else None
        if previous and len(cue['text'].split())<=2 and cue['start']-previous['end']<.35 and cue['end']-previous['start']<=4.3 and len(previous['text']+' '+cue['text'])<=70:
            previous['end']=cue['end'];previous['text']+=' '+cue['text']
        else:result.append(dict(cue))
    return result


def narrate(script: dict,out: Path) -> dict:
    if settings().get('voice_provider')=='edge':
        return asyncio.run(narrate_edge(script,out))
    return narrate_kokoro(script,out)


class IncompleteNarration(QualityBlocked):
    pass


def complete_word_timing(words,text):
    if not isinstance(words,list) or len(words)<len(text.split())*.65 or any(not isinstance(w,dict) or not isinstance(w.get('word'),str) for w in words):return False
    clean=lambda value:re.sub(r'[^\w]','',value.casefold())
    return clean(''.join(w['word'] for w in words))==clean(text)


async def collect_edge_stream(communicator,timeout_seconds=180):
    parts=[];words=[]
    # Websocket keepalives must not leave a chapter waiting indefinitely.
    async with asyncio.timeout(timeout_seconds):
        async for item in communicator.stream():
            if item['type']=='audio':parts.append(item['data'])
            elif item['type']=='WordBoundary':words.append({'start':item['offset']/1e7,'end':(item['offset']+item['duration'])/1e7,'word':html.unescape(item['text'])})
    if not parts or not words:raise IncompleteNarration('Narration or timing was not returned; no silent voice fallback.')
    return parts,words


async def narrate_edge(script: dict,out: Path) -> dict:
    import edge_tts
    import aiohttp
    cfg=settings(); cues=[]; chapters=[]; clock=0.; audio=out/'voice_parts';audio.mkdir(exist_ok=True)
    for index,chapter in enumerate(script['chapters']):
        LOG.info('Narration chapter %s/6',index+1)
        target=audio/f'{index+1:02}.mp3'; cache=target.with_suffix('.timing.json')
        key=hashlib.sha256((chapter['narration']+cfg['voice']+cfg.get('voice_rate','-4%')).encode()).hexdigest()
        stored=json.loads(cache.read_text()) if cache.exists() else {}
        if target.exists() and stored.get('key')==key and stored.get('audio_sha256')==hashlib.sha256(target.read_bytes()).hexdigest() and complete_word_timing(stored.get('words'),chapter['narration']) and duration(target)+.6>=max(w['end'] for w in stored['words']):
            words=stored['words']
        else:
            for attempt in range(3):
                parts=[];words=[]
                connector=aiohttp.TCPConnector(family=socket.AF_INET) if attempt else None
                partial=target.with_suffix('.partial.mp3')
                try:
                    communicator=edge_tts.Communicate(chapter['narration'],voice=cfg['voice'],rate=cfg.get('voice_rate','-4%'),boundary='WordBoundary',connector=connector,connect_timeout=20,receive_timeout=90)
                    parts,words=await collect_edge_stream(communicator)
                    if not complete_word_timing(words,chapter['narration']):
                        raise IncompleteNarration('Narration word timing is incomplete; retrying the whole chapter.')
                    partial.write_bytes(b''.join(parts))
                    try:
                        audio_seconds=duration(partial)
                    except (subprocess.CalledProcessError,ValueError) as error:
                        raise IncompleteNarration('Narration audio was incomplete or unreadable.') from error
                    if audio_seconds+.6<max(w['end'] for w in words):
                        raise IncompleteNarration('Narration audio ended before its last timed word.')
                    partial.replace(target)
                    save(cache,{'key':key,'audio_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'words':words})
                    break
                except (aiohttp.ClientError,asyncio.TimeoutError,IncompleteNarration,edge_tts.exceptions.NoAudioReceived):
                    if attempt==2:raise QualityBlocked('Free narration service is unavailable after bounded network retries.') from None
                    LOG.warning('Voice response incomplete or interrupted; retry %s/2',attempt+1)
                    await asyncio.sleep(2)
                finally:
                    partial.unlink(missing_ok=True)
                    if connector is not None:await connector.close()
        if not complete_word_timing(words,chapter['narration']):
            raise QualityBlocked('Narration word timing appears incomplete.')
        wav=target.with_suffix('.wav')
        command(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(target),'-c:a','pcm_s16le','-ar','48000',str(wav)])
        seconds=duration(wav)
        words=align_source_words(words,chapter['narration'])
        group=[]
        def emit(items):
            if items:cues.append({'start':clock+items[0]['start'],'end':clock+min(seconds,items[-1]['end']+.06),'text':' '.join(x['word'] for x in items)})
        for item in words:
            if group and (len(' '.join(x['word'] for x in group+[item]))>68 or item['end']-group[0]['start']>3.4):emit(group);group=[]
            group.append(item)
            if item['word'].endswith(('.', '!', '?')):emit(group);group=[]
        emit(group)
        chapters.append({'index':index,'start':clock,'end':clock+seconds});clock+=seconds
    if not cfg['minimum_seconds']<=clock<=cfg['maximum_seconds']:
        raise QualityBlocked(f'Measured narration is {clock:.1f}s, outside the long-form duration requirement. No padding or slowing.')
    filelist=audio/'concat.txt';filelist.write_text('\n'.join(f"file '{i+1:02}.wav'" for i in range(len(chapters))),encoding='utf-8')
    command(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(filelist),'-c:a','pcm_s16le','-ar','48000',str(out/'narration.wav')])
    cues=merge_orphan_cues(cues)
    for i,cue in enumerate(cues[:-1]):cue['end']=min(cue['end'],cues[i+1]['start']-.01)
    (out/'captions.srt').write_text('\n'.join(f'{i+1}\n{stamp(c["start"])} --> {stamp(c["end"])}\n{wrap(c["text"])}\n' for i,c in enumerate(cues)),encoding='utf-8-sig')
    timing={'duration':clock,'chapters':chapters,'cues':cues,'provider':'keyless Edge TTS','voice':cfg['voice']}
    save(out/'narration_timing.json',timing)
    return timing


def narrate_kokoro(script: dict, out: Path) -> dict:
    import numpy as np
    import soundfile as sf
    import espeakng_loader
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    from kokoro_onnx import Kokoro

    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    model = RUNTIME / 'voice/kokoro-v1.0.onnx'
    voices = RUNTIME / 'voice/voices-v1.0.bin'
    if not model.exists() or not voices.exists():
        raise QualityBlocked('Offline voice models are not installed.')
    engine = Kokoro(str(model), str(voices))
    cfg = settings()
    cues = []
    chapters = []
    chunks = []
    clock = 0.0
    sample_rate = None
    for index, chapter in enumerate(script['chapters']):
        start = clock
        LOG.info('Offline narration chapter %s/6', index + 1)
        for phrase in phrases(chapter['narration']):
            samples, rate = engine.create(phrase, voice=cfg['voice'], speed=cfg['voice_speed'], lang='en-us')
            if sample_rate and rate != sample_rate:
                raise QualityBlocked('Voice sample rates changed mid-program.')
            sample_rate = rate
            if len(samples) / rate < .15 or not np.isfinite(samples).all():
                raise QualityBlocked('Offline narration returned invalid audio.')
            seconds = len(samples) / rate
            cues.append({'start': clock, 'end': clock + seconds, 'text': phrase})
            chunks.extend([samples, np.zeros(round(rate * .055), dtype=np.float32)])
            clock += seconds + .055
        chapters.append({'index': index, 'start': start, 'end': clock})
    if not cfg['minimum_seconds'] <= clock <= cfg['maximum_seconds']:
        raise QualityBlocked(f'Measured narration is {clock:.1f}s; must be {cfg["minimum_seconds"]}-{cfg["maximum_seconds"]}s. No looping, slowing or silence padding.')
    audio = np.concatenate(chunks)
    sf.write(str(out / 'narration.wav'), audio, sample_rate, subtype='PCM_16')
    (out / 'captions.srt').write_text('\n'.join(f'{i+1}\n{stamp(c["start"])} --> {stamp(c["end"])}\n{wrap(c["text"])}\n' for i, c in enumerate(cues)), encoding='utf-8-sig')
    timing = {'duration': clock, 'sample_rate': sample_rate, 'chapters': chapters, 'cues': cues, 'provider': 'local Kokoro ONNX', 'voice': cfg['voice']}
    save(out / 'narration_timing.json', timing)
    return timing
