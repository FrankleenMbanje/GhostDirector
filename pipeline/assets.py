"""
GhostDirector — Asset Fetcher

Downloads REAL footage, photos, and stock clips for each scene.
Uses scene-specific search queries and tracks used URLs to ensure
every scene gets a DIFFERENT image.

Fallback order per scene:
  1. Scene-specific visual_prompt via DDG (for web_photo/photo_person)
  2. People-specific queries with variety (different search terms per scene)
  3. Stock video/photo (Pexels, Pixabay)
  4. Cross-type fallback
  5. Generic mood B-roll
  6. Styled solid-color frame (absolute last resort)
"""

import sys
import json
import re
import shutil
import random
import asyncio
import subprocess
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from models import Script, Scene
import config
from utils.logger import get_logger
from utils.retry import retry


def _vd():
    """FIX-056: lazy import of the visual director (pipeline.visual_director).

    Keeps the heavy PIL/numpy inspection machinery out of startup paths and
    avoids any circular-import issues.
    """
    from pipeline import visual_director as _visual_director
    return _visual_director


async def _gemini_illustration(
    narration: str, mood: str, width: int, height: int,
    scenes_dir: Path, scene_no: int,
) -> Path | None:
    """FIX-058: Gemini-generated contextual ILLUSTRATION for a scene whose
    real-asset fetch failed.

    Last visual resort before a bare gradient: explicitly illustrative
    (atmospheric, no photoreal people, no text) so it can never impersonate
    real footage or fabricate an event. Returns the saved JPEG path or None.
    Non-fatal by design.
    """
    if not config.GEMINI_API_KEY or not narration:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types

        prompt = (
            f"Create an atmospheric editorial illustration evoking this "
            f"narration: \"{(narration or '')[:300]}\". Mood: {mood}. "
            "Cinematic, painterly, clearly stylized — NO photorealistic "
            "people, NO faces, NO text, NO logos. Dramatic composition "
            "with strong depth. "
            + ("Vertical 9:16 composition." if height > width else "Widescreen 16:9 composition.")
        )

        def _gen():
            client = genai.Client(api_key=config.GEMINI_API_KEY)
            # FIX-058: GEMINI_IMAGE_CANDIDATES chain — image-gen ids can 404 or
            # quota-out independently (same reality as the text chain). Shape
            # retry: some ids reject IMAGE-only response_modalities and demand
            # ["TEXT","IMAGE"] (nano-banana API shape, live-verified 2026-09-24).
            shapes = (["IMAGE"], ["TEXT", "IMAGE"])
            last_err: Exception | None = None
            for model_id in config.GEMINI_IMAGE_CANDIDATES:
                for shape in shapes:
                    try:
                        resp = client.models.generate_content(
                            model=model_id,
                            contents=prompt,
                            config=genai_types.GenerateContentConfig(
                                response_modalities=list(shape),
                            ),
                        )
                        for cand in getattr(resp, "candidates", None) or []:
                            for part in getattr(cand.content, "parts", None) or []:
                                data = getattr(part, "inline_data", None)
                                if data and getattr(data, "data", None):
                                    return bytes(data.data)
                    except Exception as e:
                        last_err = e
                        if "429" in str(e):
                            break  # quota is per-model: stop burning shapes on this id
            if last_err:
                raise last_err
            return None

        out = scenes_dir / f"scene_{scene_no}_illustration.jpg"
        img = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _gen), timeout=90,
        )
        if img:
            out.write_bytes(img)
            from pipeline.visual_director import inspect_image_quality
            m = await asyncio.get_running_loop().run_in_executor(
                None, inspect_image_quality, out)
            if m.get("ok"):
                return out
            out.unlink(missing_ok=True)
    except Exception as e:
        logger.info(f"  Gemini illustration unavailable ({str(e)[:80]})")
    return None


logger = get_logger(__name__)

# Track URLs already used across scenes to prevent duplicates
_used_urls: set[str] = set()
# Permanently rejected media URLs/domains (junk stock that slipped through —
# recorded by the QC ban pass). Nothing from this set may ever ship again.
_BANNED_MEDIA: set[str] = set()
_BANNED_MEDIA_PATH = Path(config.OUTPUT_DIR) / "banned_media.json"
# Whole-domain bans: fan-art print shops (fineartamerica, artstation,
# artphotolimited…) sell paintings of celebrities, not news photos. A URL
# ban only catches the exact offender; these domains must never be sourced.
_BANNED_DOMAINS_PATH = Path(config.OUTPUT_DIR) / "banned_domains.json"
_BANNED_DOMAINS: set[str] = set()


def _is_banned_url(url: str) -> bool:
    if not url:
        return False
    if url in _BANNED_MEDIA:
        return True
    host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
    return any(host == d or host.endswith("." + d) for d in _BANNED_DOMAINS)
# B-roll cutaways already used this run — the same archive film must never
# intercut into two different scenes (the "same fruit commercial everywhere"
# failure). Reset per run alongside _used_urls.
_broll_clips: set[str] = set()


# ─────────────────────────────────────────────────
# FIX-059 — CELEBRITY VISUAL VARIETY: the celebrity stays the hero, but the
# same portrait re-zoomed across eight scenes is a slideshow fingerprint.
# A per-project ledger records a perceptual dhash of every accepted visual
# plus which person set it covered; later scenes reject near-identical
# celebrity images and rotate their query order so each scene pulls from a
# different search slice. State lives IN the project dir (like the fallback
# ledger) so resume keeps variety instead of re-picking scene 1's pick.
# ─────────────────────────────────────────────────
def _variety_state_path(output_dir: Path) -> Path:
    return Path(output_dir) / "scene_fingerprint.json"


def _load_variety_state(output_dir: Path) -> dict:
    try:
        return json.loads(
            _variety_state_path(output_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_variety_state(output_dir: Path, state: dict) -> None:
    try:
        _variety_state_path(output_dir).write_text(
            json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _image_dhash(path: Path, hash_size: int = 8) -> str:
    """Difference hash (horizontal gradient) as a hex string — robust to
    re-encode/crop-shift, catches 'same picture again'. Error → "" (never
    blocks an asset on a hashing hiccup)."""
    try:
        from PIL import Image
        img = Image.open(path).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS)
        px = list(img.getdata())
        bits = "".join(
            "1" if px[r * (hash_size + 1) + c] > px[r * (hash_size + 1) + c + 1] else "0"
            for r in range(hash_size) for c in range(hash_size))
        return "".join(
            format(int(bits[i:i + 4], 2), "x") for i in range(0, len(bits), 4))
    except Exception:
        return ""


def _dhash_distance(a: str, b: str) -> int:
    """Hamming distance between two dhash hex strings (64 = incomparable)."""
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


# dhash distances ≤ this read as "the same picture" (re-encodes land ≤4).
VARIETY_DUP_DISTANCE = 6
# A person's visual may repeat at most this many times before the selector
# MUST find a different image (2 = establishing shot + payoff callback is
# fine; 5 zooms of the same portrait is the AI-slideshow fingerprint).
VARIETY_MAX_REPEATS = 2


def _video_dhash(path: Path) -> str:
    """dhash of a video's first frame (identity for stock/clip videos)."""
    try:
        import subprocess as _sp
        import tempfile as _tempfile
        tf = _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp = Path(tf.name)
        tf.close()
        r = _sp.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-frames:v", "1", str(tmp)],
            capture_output=True, timeout=30,
        )
        h = _image_dhash(tmp) if (r.returncode == 0 and tmp.exists()) else ""
        tmp.unlink(missing_ok=True)
        return h
    except Exception:
        return ""


def _person_key(people: list | None) -> str | None:
    """Stable key for a scene's person set ('brad pitt'), or None."""
    try:
        names = sorted(
            " ".join(str(p).lower().split()) for p in (people or []) if str(p).strip()
        )
        names = [n for n in names if n]
        return " ".join(names) or None
    except Exception:
        return None


def _scene_visual_repeat(
    state: dict,
    people: list | None,
    new_hash: str,
) -> int:
    """How many ACCEPTED scenes of the same person set this visual nearly
    duplicates (0 = fresh). Errors count as fresh — variety is best-effort."""
    try:
        key = _person_key(people)
        if not key or not new_hash:
            return 0
        n = 0
        for rec in (state.get("scenes") or {}).values():
            if rec.get("person_key") != key:
                continue
            if _dhash_distance(new_hash, rec.get("hash", "")) <= VARIETY_DUP_DISTANCE:
                n += 1
        return n
    except Exception:
        return 0


def _register_scene_visual(
    output_dir: Path,
    scene_no: int,
    people: list | None,
    media_path: Path,
    source: str = "",
    visual_type: str = "",
) -> None:
    """Record an accepted scene visual in the per-project variety ledger."""
    try:
        state = _load_variety_state(output_dir)
        p = Path(media_path)
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            h = _image_dhash(p)
        else:
            h = _video_dhash(p)
        state.setdefault("scenes", {})[str(scene_no)] = {
            "person_key": _person_key(people),
            "hash": h,
            "source": source,
            "visual_type": visual_type,
        }
        _save_variety_state(output_dir, state)
    except Exception:
        pass


def _rotate_person_queries(queries: list[str], scene_no: int) -> list[str]:
    """Rotate the query list per scene so consecutive scenes search from a
    different slice of the same person's results (portrait / hd / event
    shots lead in turn instead of the same top hit every time)."""
    try:
        if not queries:
            return queries
        k = (int(scene_no) - 1) % max(len(queries), 1)
        return queries[k:] + queries[:k]
    except Exception:
        return queries

# ─────────────────────────────────────────────────
# Watermarked-source blocklist (FIX-049): stock-agency PREVIEW images ship
# with visible "stock"/logo stamps. A watermarked frame on screen screams
# low-effort slideshow. DDG happily returns these, so they are rejected at
# the URL level before download is even attempted.
# ─────────────────────────────────────────────────
WATERMARK_DOMAINS = (
    "alamy.", "alamy.com", ".alamy",
    "shutterstock.", "shutterstock.com",
    "gettyimages.", "gettyimages.com", "media.gettyimages",
    "freepik.", "freepik.com", "img.freepik",
    "dreamstime.", "dreamstime.com",
    "123rf.", "123rf.com",
    "istockphoto.", "istockphoto.com", "media.istockphoto",
    "depositphotos.", "depositphotos.com",
    "agefotostock.", "agefotostock.com",
    "stock.adobe", "adobe.stock",
    "bigstock",
    "pond5", "storyblocks", "videoblocks",
    "colourbox", "photodune", "graphicriver",
)


def is_watermarked_source(url: str | None) -> bool:
    """True when a URL belongs to a stock-agency preview (visible watermark)."""
    if not url:
        return False
    u = url.lower()
    return any(dom in u for dom in WATERMARK_DOMAINS)


# ─────────────────────────────────────────────────
# Archive.org public-domain footage (FIX-050): real historical film for
# biography/documentary topics. Pre-1930 motion picture collections on
# archive.org are public domain — REAL footage of Rockefeller-era America,
# not stills and not watermarked stock. Downloaded as short muted excerpts
# used as b-roll cutaways.
# ─────────────────────────────────────────────────
_ARCHIVE_CACHE: dict[str, list[dict]] = {}


def search_archive_footage(query: str, max_results: int = 8) -> list[dict]:
    """Search archive.org for public-domain moving images matching a query.

    Uses the advancedsearch API scoped to the 'movingimages' / pre-1930 film
    collections. Returns [{id, title, description}] — callers resolve a
    playable file URL per item via _archive_video_url().
    """
    q = (query or "").strip()
    if not q:
        return []
    if q in _ARCHIVE_CACHE:
        return _ARCHIVE_CACHE[q]

    try:
        url = (
            "https://archive.org/advancedsearch.php?"
            + urllib.parse.urlencode({
                "q": f"{q} AND mediatype:(movies) AND year:[* TO 1928]",
                "fl[]": "identifier,title,description",
                "rows": str(max_results),
                "output": "json",
            })
        )
        r = httpx.get(url, timeout=15.0, headers={"User-Agent": "GhostDirectorStudio/2.5"})
        r.raise_for_status()
        data = r.json()
        docs = (data.get("response") or {}).get("docs") or []
        # Relevance gate: archive.org fulltext-matches loosely (a Tom Cruise
        # query surfaced "Some Fruits We Like" because the word "cruise"
        # appeared in its description). Require every distinctive query
        # token (≥4 chars) to appear in the item's title — a silent-era
        # film whose title shares no word with the query is NEVER the right
        # cutaway, no matter what the fulltext engine claims.
        tokens = {t for t in re.split(r"\W+", q.lower()) if len(t) >= 4}
        items = []
        for d in docs:
            if not d.get("identifier"):
                continue
            title = (d.get("title") or "").lower()
            if tokens and not tokens.issubset(set(re.split(r"\W+", title))):
                continue
            items.append({"id": d["identifier"], "title": d.get("title", "")})
        _ARCHIVE_CACHE[q] = items
        return items
    except Exception as e:
        logger.warning(f"archive.org search failed for '{q}': {e}")
        _ARCHIVE_CACHE[q] = []
        return []


def _archive_video_url(item_id: str) -> str | None:
    """Resolve a playable mp4/ogv URL for an archive.org item.

    Prefers SMALL derivative files (512kb/h.264 derivatives are near-universal
    on archive.org): range-request stream-cutting a 1GB preservation master
    is what made remote cuts time out — a 50-120MB derivative seeks instantly.
    """
    try:
        r = httpx.get(
            f"https://archive.org/metadata/{item_id}", timeout=12.0,
            headers={"User-Agent": "GhostDirectorStudio/2.5"},
        )
        r.raise_for_status()
        data = r.json()
        files = data.get("files", [])
        cands = [f for f in files if f.get("name", "").lower().endswith(".mp4")]
        if not cands:
            # fall back to any playable video container
            cands = [f for f in files if f.get("name", "").lower().endswith((".ogv", ".webm"))]
        if not cands:
            return None

        def _sz(f):
            try:
                return int(f.get("size") or 0)
            except (TypeError, ValueError):
                return 0

        small = [f for f in cands if 0 < _sz(f) <= 150_000_000] or cands
        small.sort(key=_sz)
        best = small[0].get("name", "")
        if not best:
            return None
        return f"https://archive.org/download/{item_id}/{urllib.parse.quote(best)}"
    except Exception as e:
        logger.warning(f"archive.org metadata lookup failed for {item_id}: {e}")
        return None

# ─────────────────────────────────────────────────
# B-roll memory (Phase 2.8): persistence across videos
# ─────────────────────────────────────────────────
# Used stock queries/URLs live in db/used_broll.json so consecutive uploads
# rotate through the library instead of re-using the same first Pexels hit
# or DDG image for popular queries. Keep it bounded — this is a rotation
# ledger, not an archive.
_BROLL_MEMORY_PATH = config.DB_DIR / "used_broll.json"
_BROLL_MEMORY_MAX = 2000
_BROLL_DECAY_DAYS = 90   # unused this long → usage penalty expires (rotation restarts)
_broll_memory: list[dict] = []


def _load_used_broll() -> list[dict]:
    try:
        data = json.loads(_BROLL_MEMORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"Could not read {_BROLL_MEMORY_PATH.name} ({e}); starting fresh")
        return []


def _decay_entries(entries: list[dict]) -> list[dict]:
    """Drop usage weights older than the decay window (FIX-027).

    A query unused for _BROLL_DECAY_DAYS days gets its penalty expired so the
    rotation pool doesn't permanently deprioritize early queries. The entry
    itself survives (URL history stays useful); only its count contribution
    ages out.
    """
    cutoff = datetime.now() - timedelta(days=_BROLL_DECAY_DAYS)
    kept: list[dict] = []
    for it in entries:
        try:
            last = datetime.fromisoformat(it.get("last_used", ""))
            if last < cutoff:
                it = {**it, "decayed": True}  # marker; _query_usage skips it
        except (ValueError, TypeError):
            pass
        kept.append(it)
    return kept


def _save_used_broll() -> None:
    try:
        _BROLL_MEMORY_PATH.write_text(
            json.dumps(_broll_memory[-_BROLL_MEMORY_MAX:], indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Could not save b-roll memory: {e}")


def _record_used(scene: Scene, query: str) -> None:
    """Add a scene's final asset to the cross-video rotation ledger."""
    url = (scene.source_url or "").strip()
    if not url:
        return
    entry = {
        "url": url,
        "title": (scene.source_title or "").strip(),
        "channel": (scene.source_channel or "").strip(),
        "query": (query or "").strip(),
        "type": scene.visual_type,
        "last_used": datetime.now().isoformat(timespec="seconds"),
    }
    for it in _broll_memory:
        if it.get("url") == url:
            # Backfill previously-empty fields too (an entry recorded without
            # query/provenance can gain them later); never overwrite with empty.
            for k, v in entry.items():
                if v:
                    it[k] = v
            return
    _broll_memory.append(entry)
    if len(_broll_memory) > _BROLL_MEMORY_MAX:
        del _broll_memory[: len(_broll_memory) - _BROLL_MEMORY_MAX]


def _query_usage() -> dict[str, int]:
    """How many recent videos used each query — for Tier-3 rotation.

    Entries decayed past the window (FIX-027) don't count against a query,
    so a pool regains availability after 90 idle days.
    """
    counts: dict[str, int] = {}
    for it in _broll_memory:
        if it.get("decayed"):
            continue
        q = (it.get("query") or "").strip().lower()
        if q:
            counts[q] = counts.get(q, 0) + 1
    return counts


# ─────────────────────────────────────────────────
# Download helper
# ─────────────────────────────────────────────────

def valid_video(path: Path, min_w: int = 640, min_h: int = 360) -> bool:
    """Quality gate for VIDEO b-roll/stock (the photo gate's twin).

    Rejects: corrupt/tiny files, ultra-low resolution, washed-out or
    near-black footage (mean luma outside 48–235, no sample below 30), and
    flat frames (luma stddev < 16) — the cheap-stock-footage look on a
    phone screen.
    """
    if not path.exists() or path.stat().st_size <= 50000:
        return False
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
             "-select_streams", "v:0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode != 0:
            return False
        streams = json.loads(probe.stdout or "{}").get("streams") or []
        if not streams:
            return False
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        if w < min_w or h < min_h:
            return False
        # Sample brightness/contrast at 5 spread points via signalstats.
        dur = 0.0
        probe2 = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if probe2.returncode == 0:
            dur = float(json.loads(probe2.stdout or "{}").get("format", {}).get("duration") or 0)
        points = [dur * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)] if dur > 1 else [0.0]
        means: list[float] = []
        stds: list[float] = []
        for t in points:
            r = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=15,
            )
            m = re.search(r"YAVG=([\d.]+)", r.stdout or "")
            if not m:
                continue
            means.append(float(m.group(1)))
            r2 = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YSTD:file=-",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=15,
            )
            m2 = re.search(r"YSTD=([\d.]+)", r2.stdout or "")
            if m2:
                stds.append(float(m2.group(1)))
        if not means:
            return True  # couldn't sample; don't veto on tooling failure
        mean_luma = sum(means) / len(means)
        std_luma = (sum(stds) / len(stds)) if stds else 40.0
        # Floors are per-sample AND average: a clip averaging bright can still
        # carry near-black sections that swallow captions on a phone screen.
        if mean_luma < 48 or mean_luma > 235 or min(means) < 30 or std_luma < 16:
            return False
        return True
    except Exception:
        return False


def valid_photo(path: Path, min_w: int = 1280, min_h: int = 720) -> bool:
    """Quality gate: photo exists, isn't tiny, has broadcast-grade resolution,
    AND is visually usable (contrast floor + flatness check).
    Rejects low-res thumbnails, extreme banner collages, corrupt images, and
    washed-out/flat frames that read as cheap on a phone screen."""
    if not path.exists() or path.stat().st_size <= 10000:
        return False
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            w, h = im.size
            ratio = w / float(h) if h else 0
            # Reject extreme aspect ratios (collages, ultra-wide banners, vertical strips)
            if ratio > 2.6 or ratio < 0.38:
                return False
            # Check either landscape min_w x min_h or portrait min_h x min_w
            is_landscape = (w >= min_w and h >= min_h)
            is_portrait = (h >= min_w and w >= min_h)
            # Accept if at least 1000 in long dimension and 600 in short
            acceptable_res = (max(w, h) >= 1000 and min(w, h) >= 600)
            if not (is_landscape or is_portrait or acceptable_res):
                return False
            # Contrast floor: near-uniform frames (logos, blank cards, faint
            # scans) look broken under Ken Burns. Luma stddev < 12 = flat.
            stat = ImageStat.Stat(im.convert("L").resize((160, 90)))
            if stat.stddev[0] < 12.0:
                return False
            return True
    except Exception:
        return False


def ground_visual_query(query: str, broll_keywords: list[str] | None = None) -> str:
    """
    Translates abstract emotional concepts into concrete physical objects/actions.
    Uses broll_keywords if provided by the scriptwriter.
    """
    if broll_keywords and len(broll_keywords) > 0:
        return broll_keywords[0]
    abstract_map = {
        "greed": "stacks of cash money counting",
        "betrayal": "secret meeting whispering silhouette",
        "regret": "person hands on head looking down",
        "power": "executive high rise office desk skyline",
        "collapse": "demolition falling building ruins",
        "dread": "dark stormy clouds lightning cinematic",
        "success": "luxury sports car mansion celebration",
        "secrets": "locked safe classified document stamped",
        "arrest": "police officer handcuffs flashing sirens",
        "conspiracy": "surveillance camera document evidence board",
    }
    q_lower = (query or "").lower()
    for word, concrete in abstract_map.items():
        if word in q_lower:
            return concrete
    return query


async def download_file(url: str, output_path: Path, timeout: float = 90.0):
    """Download a file. Skip if already cached.

    FIX-050.2: hard asyncio deadline. httpx's read timeout can be defeated
    by a CDN that trickles keepalive bytes (observed: socket stuck in
    CLOSE_WAIT for 9+ minutes with no read event); wait_for kills the whole
    operation at the cap regardless of socket state.

    FIX-057: default cap 60s → 90s. Live repair runs showed a 25MB 1080p
    b-roll legitimately needing ~70s on a real home network — the 60s cap
    was converting slow-but-fine downloads into fallback gradients, which
    the strict QC then (correctly) refuses. Per-call `timeout` lets fast
    paths stay tight.
    """
    if output_path.exists() and output_path.stat().st_size > 0:
        logger.info(f"Cached: {output_path.name}")
        return

    logger.info(f"Downloading -> {output_path.name}")

    async def _do():
        async with httpx.AsyncClient(follow_redirects=True, timeout=max(timeout, 90.0)) as client:
            response = await client.get(url)
            response.raise_for_status()
            output_path.write_bytes(response.content)

    try:
        await asyncio.wait_for(_do(), timeout=timeout)
    except asyncio.TimeoutError:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"download exceeded {timeout:.0f}s cap: {url[:80]}")


# ─────────────────────────────────────────────────
# DuckDuckGo Image Search (REAL photos)
# ─────────────────────────────────────────────────

async def search_duckduckgo_photos(query: str, max_results: int = 10) -> list[str]:
    """Search DDG for REAL photos, best-resolution first.

    Results are sorted by reported width (descending) so scene photo slots
    and the thumbnail background get the sharpest available source instead
    of whatever loaded first. Watermarked stock-agency previews are rejected
    at the URL level (FIX-049) — they used to sail through with visible
    "stock" stamps on screen.
    Returns list of image URLs."""
    def _sync():
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return []
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(query, max_results=max_results * 2))
                # Prefer high-resolution sources (FIX-013): sort candidates by
                # reported width so we download the sharpest image, not the
                # first one DDG happened to return. FIX-056b: DDG reports
                # width as string OR int depending on the day — coerce both,
                # or the sort crashes ('<' not supported between int and str)
                # and the whole celebrity-photo source dies.
                def _w(r):
                    try:
                        return int(r.get("width") or 0)
                    except (TypeError, ValueError):
                        return 0
                results.sort(key=_w, reverse=True)
                # FIX-049: drop watermarked stock-agency previews entirely;
                # QC ban pass: never return banned URLs/domains (fan-art
                # print shops shipped a watermarked painting as "news")
                clean = [r.get("image") for r in results
                         if r.get("image") and not is_watermarked_source(r.get("image"))
                         and not _is_banned_url(r.get("image"))]
                return clean[:max_results] or [r.get("image") for r in results if r.get("image")][:max_results]
        except Exception as e:
            logger.warning(f"DDG image search failed for '{query}': {e}")
            return []
    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def search_duckduckgo_unique(query: str) -> str | None:
    """Search DDG and return the first URL that hasn't been used yet."""
    urls = await search_duckduckgo_photos(query, max_results=10)
    for url in urls:
        if url not in _used_urls and not _is_banned_url(url):
            _used_urls.add(url)
            return url
    # If all 10 are used, return first one anyway
    return urls[0] if urls else None


async def search_wikimedia_portrait(person_name: str) -> str | None:
    """
    Fetch official high-resolution historical portrait from Wikimedia Commons / Wikipedia.
    Guarantees authentic, iconic faces for real people (never technical diagrams or wrong stock).
    """
    if not person_name or len(person_name.strip()) < 3:
        return None
    clean_name = person_name.strip()
    try:
        url = f"https://en.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(clean_name)}&prop=pageimages&format=json&pithumbsize=1920"
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "GhostDirectorStudio/2.5 (media@ghostdirector.ai)"})
            if r.status_code == 200:
                data = r.json()
                pages = data.get("query", {}).get("pages", {})
                for pid, page in pages.items():
                    thumb = page.get("thumbnail", {}).get("source")
                    if thumb and thumb not in _used_urls:
                        _used_urls.add(thumb)
                        return thumb
    except Exception as e:
        logger.warning(f"Wikimedia search failed for '{person_name}': {e}")
    return None


# ─────────────────────────────────────────────────
# Pexels API (with candidate fallback loop)
# ─────────────────────────────────────────────────

@retry(max_attempts=2, base_delay=1.0)
async def search_pexels_video(query: str) -> str | None:
    """Backward-compatible best single hit = first of the ranked candidates."""
    cands = await search_pexels_video_candidates(query, per_query=1)
    return cands[0][0] if cands else None


async def search_pexels_video_candidates(query: str, per_query: int = 5) -> list[tuple[str, int]]:
    """FIX-056: ranked Pexels VIDEO candidates (best file per result video).

    Returns [(url, pixel_count), ...] ordered best-first. Only HD-or-better
    files qualify (>=1280x720); banned/used URLs are filtered up front. The
    caller inspects pixels and picks — no more settling on the first hit.
    """
    if not config.PEXELS_API_KEY:
        return []
    url = f"https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}&per_page=15&orientation=landscape"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"Authorization": config.PEXELS_API_KEY})
        r.raise_for_status()
        data = r.json()
    out: list[tuple[str, int]] = []
    for vid in data.get("videos", []):
        # Pick the file CLOSEST TO 1080p, not the largest — the old
        # largest-file choice grabbed 4K originals (30-80MB) that blew the
        # download budget for a 7-second 1080p b-roll cut. 1080p is the
        # delivery ceiling; 720p is the floor.
        best, best_score = None, None
        for f in vid.get("video_files", []):
            w, h = f.get("width", 0), f.get("height", 0)
            link = f.get("link")
            if w and h and link and link not in _used_urls and not _is_banned_url(link):
                px = w * h
                if px >= 1280 * 720:
                    score = abs(px - 1920 * 1080)
                    if best_score is None or score < best_score:
                        best, best_score = link, score
        if best and best_score is not None:
            out.append((best, 1920 * 1080 - best_score))
    out.sort(key=lambda t: -t[1])
    return out[:per_query]


@retry(max_attempts=2, base_delay=1.0)
async def search_pexels_photo(query: str) -> str | None:
    """Backward-compatible best single hit = first of the ranked candidates."""
    cands = await search_pexels_photo_candidates(query, per_query=1)
    return cands[0] if cands else None


async def search_pexels_photo_candidates(query: str, per_query: int = 5) -> list[str]:
    """FIX-056: ranked Pexels PHOTO candidates, highest-res first.

    Requests large2x (≈1880px) sources so every candidate starts above the
    720p floor; the caller still inspects actual pixels before choosing.
    """
    if not config.PEXELS_API_KEY:
        return []
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=15&orientation=landscape"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"Authorization": config.PEXELS_API_KEY})
        r.raise_for_status()
        data = r.json()
    photos = data.get("photos", [])
    out: list[str] = []
    seen: set[str] = set()
    for photo in photos:
        src = photo.get("src", {})
        link = src.get("large2x") or src.get("original") or src.get("large")
        if (link and link not in _used_urls and link not in seen
                and not _is_banned_url(link)):
            seen.add(link)
            out.append(link)
    return out[:per_query]


# ─────────────────────────────────────────────────
# Pixabay API (with candidate fallback loop)
# ─────────────────────────────────────────────────

@retry(max_attempts=2, base_delay=1.0)
async def search_pixabay_video(query: str) -> str | None:
    if not config.PIXABAY_API_KEY:
        return None
    url = f"https://pixabay.com/api/videos/?key={config.PIXABAY_API_KEY}&q={urllib.parse.quote(query)}&per_page=10"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    hits = data.get("hits", [])
    for hit in hits:
        link = hit.get("videos", {}).get("large", {}).get("url") or hit.get("videos", {}).get("medium", {}).get("url")
        if link and link not in _used_urls:
            _used_urls.add(link)
            return link
    return None


@retry(max_attempts=2, base_delay=1.0)
async def search_pixabay_photo(query: str) -> str | None:
    if not config.PIXABAY_API_KEY:
        return None
    url = f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={urllib.parse.quote(query)}&image_type=photo&per_page=10"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    hits = data.get("hits", [])
    for hit in hits:
        link = hit.get("largeImageURL")
        if link and link not in _used_urls:
            _used_urls.add(link)
            return link
    return None


# ─────────────────────────────────────────────────
# YouTube clip scraper
# ─────────────────────────────────────────────────

def _fmt_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS for yt-dlp --download-sections."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def download_youtube_clip(
    query: str,
    output_path: Path,
    max_clip_seconds: float = 6.0,
    seed: int = 0,
) -> dict | None:
    """
    Download a SHORT excerpt from the top YouTube search result.

    Copyright policy (UPGRADE_PLAN.md §2, hardened FIX-050): the excerpt is
    capped at 6 SECONDS (the widely-used short-excerpt convention), the clip
    is downloaded VIDEO-ONLY — the source audio track is never kept, because
    Content ID flags on AUDIO matching far more often than on picture — the
    start time varies deterministically per scene so every video samples a
    different section, and source metadata is captured to the Scene for the
    credit ledger.

    Returns {"url", "title", "channel"} for provenance, or None on failure.
    """
    source_cache = output_path.with_suffix(".source.json")
    if output_path.exists() and output_path.stat().st_size > 0:
        # Cached clip — restore provenance if we saved it earlier
        if source_cache.exists():
            try:
                return json.loads(source_cache.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    logger.info(f"Scraping YouTube clip (max {max_clip_seconds:.0f}s): '{query}'...")

    def _probe() -> dict | None:
        """Fetch metadata for the top search result without downloading."""
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--dump-single-json", "--no-playlist", "--quiet",
            f"ytsearch1:{query}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    try:
        meta = await asyncio.get_running_loop().run_in_executor(None, _probe)
    except Exception as e:
        logger.warning(f"yt-dlp probe failed: {e}")
        return None

    if not meta:
        return None

    duration = meta.get("duration") or 0
    if duration and duration <= max_clip_seconds + 30:
        logger.warning(f"  Source too short ({duration:.0f}s): {meta.get('title', '?')[:50]}")
        return None

    # Varied, deterministic start: between 45s and (duration - 20s - clip).
    # Falls back to ~60s when duration is unknown.
    lo = 45.0
    hi = (duration - 20.0 - max_clip_seconds) if duration else 60.0
    if hi <= lo:
        hi = lo
    start = random.Random(f"{seed}:{query}").uniform(lo, hi)
    end = start + max_clip_seconds

    url = meta.get("webpage_url") or meta.get("original_url") or f"ytsearch1:{query}"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        # FIX-050: VIDEO-ONLY formats — no +bestaudio, so the downloaded file
        # has no audio track at all. Muted source + ≤6s excerpt + per-cut fair-
        # use transforms + credit ledger = the safe-excerpt recipe.
        "-f", "bestvideo[ext=mp4][height<=1080]/bestvideo[height<=1080]/bestvideo/best[ext=mp4][height<=1080]",
        "--download-sections", f"*{_fmt_hms(start)}-{_fmt_hms(end)}",
        "--force-keyframes-at-cuts",
        "--no-playlist", "--quiet",
        "-o", str(output_path),
        url,
    ]

    def _run() -> bool:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 and result.stderr:
            logger.warning(f"  yt-dlp: {result.stderr.strip().splitlines()[-1][:120]}")
        return result.returncode == 0

    try:
        ok = await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception as e:
        logger.warning(f"yt-dlp failed: {e}")
        return None

    if not ok or not output_path.exists() or output_path.stat().st_size == 0:
        return None

    provenance = {
        "url": meta.get("webpage_url", ""),
        "title": meta.get("title", ""),
        "channel": meta.get("uploader") or meta.get("channel", ""),
    }
    try:
        source_cache.write_text(json.dumps(provenance, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return provenance


# ─────────────────────────────────────────────────
# Fallback clip (NEVER show black)
# ─────────────────────────────────────────────────

MOOD_COLORS = {
    "dramatic":    "1a1a2e",
    "suspenseful": "0f0c29",
    "dark":        "0d0d0d",
    "emotional":   "2d1b69",
    "upbeat":      "1a3a5c",
    "neutral":     "1c1c1c",
    "triumphant":  "1a1a2e",
}

def generate_fallback_clip(output_path: Path, duration: float, mood: str,
                           text: str = "", width: int = 1920, height: int = 1080):
    """Generate a styled solid color clip with optional quote text."""
    c1 = MOOD_COLORS.get(mood, "1a1a2e")
    vf = ""
    if text:
        safe_text = text[:80].replace("'", "").replace(":", " -").replace("\\", "")
        vf = (
            f"drawtext=text='{safe_text}':"
            f"fontcolor=white:fontsize=52:x=(w-text_w)/2:y=(h-text_h)/2:"
            f"borderw=2:bordercolor=black"
        )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i", f"color=c=0x{c1}:s={width}x{height}:d={duration}:r=30",
    ]
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend([
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-pix_fmt", "yuv420p", str(output_path),
    ])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Fallback clip generation failed: {result.stderr}")


# ─────────────────────────────────────────────────
# Search helpers
# ─────────────────────────────────────────────────

async def _try_video_stock(
    queries: list[str],
    narration: str = "",
    mood: str = "neutral",
    people: list[str] | None = None,
    target_size: tuple[int, int] = (1920, 1080),
) -> str | None:
    """FIX-056: candidate-pool stock video search with the visual director's
    inspect→compare→select loop (was: first URL that survived the probe)."""
    tmp = Path(config.OUTPUT_DIR) / "_vq_probe.mp4"
    for q in queries:
        if not q:
            continue
        cands: list[str] = [u for u, _px in await search_pexels_video_candidates(q, per_query=5)]
        if not cands:
            cands = [u for u in [await search_pixabay_video(q)] if u]
        if not cands:
            continue
        chosen, _ev = await _vd().select_best_visual(
            cands, narration=narration or q, mood=mood, people=people or [],
            target_size=target_size, kind="video", max_candidates=3,
            already_used=(_BANNED_MEDIA | set()),
        )
        if not chosen:
            continue
        if tmp is not None:
            try:
                await download_file(chosen, tmp)
                if valid_video(tmp):
                    _used_urls.add(chosen)
                    return chosen
            except Exception:
                pass
        else:
            _used_urls.add(chosen)
            return chosen
    return None


async def _try_photo_stock(
    queries: list[str],
    narration: str = "",
    mood: str = "neutral",
    people: list[str] | None = None,
    target_size: tuple[int, int] = (1920, 1080),
) -> str | None:
    """FIX-056: candidate-pool stock photo search with inspect→compare→select."""
    for q in queries:
        if not q:
            continue
        cands = await search_pexels_photo_candidates(q, per_query=5)
        if not cands:
            px = await search_pixabay_photo(q)
            cands = [px] if px else []
        if not cands:
            continue
        chosen, _ev = await _vd().select_best_visual(
            cands, narration=narration or q, mood=mood, people=people or [],
            target_size=target_size, kind="photo", max_candidates=4,
            already_used=(_BANNED_MEDIA | set()),
        )
        if chosen:
            _used_urls.add(chosen)
            return chosen
    return None


GENERIC_BROLL = {
    "dramatic":    ["dramatic cinematic background", "dark sky clouds timelapse"],
    "suspenseful": ["dark hallway shadow", "suspenseful noir atmosphere"],
    "dark":        ["dark abstract smoke", "moody fog background"],
    "emotional":   ["emotional sunset silhouette", "person looking out window rain"],
    "upbeat":      ["bright city lights", "celebration crowd energy"],
    "neutral":     ["abstract minimalist background", "modern office building"],
    "triumphant":  ["golden trophy spotlight", "sunrise mountain peak"],
}


# ─────────────────────────────────────────────────
# Main fetch function
# ─────────────────────────────────────────────────

def _cut_remote_segment(
    remote_url: str, output_path: Path, seed: int = 0,
    clip_seconds: float = 8.0,
) -> bool:
    """Stream-cut a short MUTED excerpt from a remote video over HTTP.

    Range-request input seeking keeps the transfer to a few MB instead of
    pulling whole archive films. Audio is stripped (-an): archive footage is
    used silent under our own VO + music bed (and silent sources can never
    trip audio-matching). Output is normalized to 720p pillarboxed so 4:3
    historical film keeps its frame.
    """
    if output_path.exists() and output_path.stat().st_size > 0:
        return True
    duration = 0.0
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", remote_url],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            duration = float(json.loads(probe.stdout).get("format", {}).get("duration") or 0)
    except Exception:
        duration = 0.0
    if duration > clip_seconds + 40:
        lo, hi = 20.0, duration - clip_seconds - 5.0
    else:
        lo, hi = 5.0, 45.0
    start = random.Random(f"archive:{seed}").uniform(lo, hi)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.2f}", "-i", remote_url,
        "-t", f"{clip_seconds:.2f}", "-an",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", str(output_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        ok = r.returncode == 0 and output_path.exists() and output_path.stat().st_size > 50000
        if ok:
            try:
                from utils.ffmpeg_cmd import get_duration
                if get_duration(output_path) < 1.5:
                    ok = False
            except Exception:
                pass
        if not ok:
            output_path.unlink(missing_ok=True)
        return ok
    except Exception as e:
        logger.warning(f"archive segment cut failed: {e}")
        output_path.unlink(missing_ok=True)
        return False


async def fetch_public_domain_footage(
    people: list[str], primary: str, output_path: Path, seed: int = 0,
) -> dict | None:
    """Cut a short public-domain excerpt from archive.org for a scene.

    Tries person names first (their era's newsreels), then the scene's own
    visual prompt. Returns provenance for the credit ledger, or None.
    """
    if output_path.exists() and output_path.stat().st_size > 0:
        sidecar = output_path.with_suffix(".source.json")
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    loop = asyncio.get_running_loop()
    queries: list[str] = [p for p in (people or []) if p and len(p.strip()) > 2]
    p = (primary or "").strip()
    if p and p not in queries:
        queries.append(p)

    # FIX-050.1: hard per-scene time budget. Archive.org throughput varies
    # wildly; without a cap one throttled film eats minutes of every scene.
    # After the budget expires the scene degrades to stock b-roll instantly.
    import time as _time
    deadline = _time.time() + 45.0

    for q in queries:
        items = await loop.run_in_executor(None, search_archive_footage, q)
        for item in items[:2]:
            if _time.time() > deadline:
                return None
            vid_url = await loop.run_in_executor(None, _archive_video_url, item["id"])
            if not vid_url:
                continue
            ok = await loop.run_in_executor(
                None, _cut_remote_segment, vid_url, output_path, seed + len(q))
            if ok:
                title = item.get("title") or item["id"]
                if title in _broll_clips:   # already intercut elsewhere this run
                    output_path.unlink(missing_ok=True)
                    output_path.with_suffix(".source.json").unlink(missing_ok=True)
                    continue
                prov = {
                    "url": vid_url,
                    "title": title,
                    "channel": "Internet Archive (public domain)",
                }
                if not valid_video(output_path):
                    output_path.unlink(missing_ok=True)
                    continue
                try:
                    output_path.with_suffix(".source.json").write_text(
                        json.dumps(prov, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                _broll_clips.add(title)
                logger.info(f"  🎞️ PD footage: '{(prov['title'] or '')[:50]}' (archive.org)")
                return prov
    return None


async def _pair_broll_cutaway(
    scene, people: list[str], primary: str, scenes_dir: Path, sn: int,
) -> None:
    """Fetch a paired cutaway clip for a photo scene (FIX-050).

    The VidRush look alternates the main shot with REAL footage; a photo
    scene can only alternate framings of one still. A short public-domain
    (archive.org) or stock clip stored as `broll_video_path` gives the cut
    engine a second source to intercut — main shot / cutaway / main.
    """
    if not (scene.photo_path and Path(scene.photo_path).exists()):
        return
    if scene.broll_video_path and Path(scene.broll_video_path).exists():
        return
    broll_path = scenes_dir / f"scene_{sn}_broll.mp4"
    prov = await fetch_public_domain_footage(people, primary, broll_path, seed=sn * 7 + 1)
    if not prov:
        broll_kw = getattr(scene, "broll_keywords", []) or []
        grounded = ground_visual_query(primary, broll_kw)
        queries = [grounded] + [k for k in broll_kw if k != grounded]
        url = await _try_video_stock(
            queries[:3], narration=scene.narration, mood=scene.mood or "neutral",
            people=people,
        )
        if url:
            try:
                await download_file(url, broll_path)
                prov = {"url": url, "title": f"Stock b-roll: {queries[0]}", "channel": "Pexels/Pixabay"}
            except Exception:
                prov = None
    if prov:
        scene.broll_video_path = str(broll_path)
        logger.info(f"  🎞️ Cutaway paired: '{(prov.get('title') or '')[:45]}'")


def ban_media(urls: list[str], domains: list[str] | None = None) -> int:
    """Permanently blacklist junk media URLs and/or whole domains (QC ban pass)."""
    global _BANNED_MEDIA
    try:
        existing = set(json.loads(_BANNED_MEDIA_PATH.read_text(encoding="utf-8")))
    except Exception:
        existing = set()
    merged = existing | set(u for u in urls if u)
    _BANNED_MEDIA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BANNED_MEDIA_PATH.write_text(json.dumps(sorted(merged), indent=1), encoding="utf-8")
    _BANNED_MEDIA = merged
    added_domains = 0
    if domains:
        try:
            existing_d = set(json.loads(_BANNED_DOMAINS_PATH.read_text(encoding="utf-8")))
        except Exception:
            existing_d = set()
        merged_d = existing_d | {d.lower().strip() for d in domains if d.strip()}
        _BANNED_DOMAINS_PATH.write_text(json.dumps(sorted(merged_d), indent=1), encoding="utf-8")
        added_domains = len(merged_d - existing_d)
        _BANNED_DOMAINS = merged_d
    return len(merged - existing) + added_domains


async def fetch_assets(script: Script, template: dict, output_dir: Path) -> Script:
    """
    Fetch assets for each scene with cascading fallback.

    Every scene gets a DIFFERENT image because:
    1. Each scene's visual_prompt is unique and scene-specific
    2. We track _used_urls globally and skip duplicates
    3. DDG returns 10 results and we pick the first unused one
    """
    global _used_urls, _broll_clips, _BANNED_MEDIA, _BANNED_DOMAINS
    _used_urls = set()  # Reset for each run
    # Load the permanent junk-media blacklist (QC ban pass writes it)
    try:
        _BANNED_MEDIA = set(json.loads(_BANNED_MEDIA_PATH.read_text(encoding="utf-8")))
    except Exception:
        _BANNED_MEDIA = set()
    try:
        _BANNED_DOMAINS = {d.lower() for d in json.loads(_BANNED_DOMAINS_PATH.read_text(encoding="utf-8"))}
    except Exception:
        _BANNED_DOMAINS = {"fineartamerica.com", "artstation.com", "artphotolimited.com",
                            "pixels.com", "redbubble.com", "deviantart.com"}
    _broll_clips = set()  # Same clip must never cutaway two scenes
    global _broll_memory
    _broll_memory = _decay_entries(_load_used_broll())  # Cross-video ledger + decay (FIX-027)
    prior_usage = _query_usage()

    scenes_dir = output_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    # FIX-057: Tier-4 fallback ledger — a gradient scene is NOT visually
    # complete. Reset per run (each run re-marks what actually fell back);
    # the final QC gate reads this to block silent fallback shipping.
    _fallback_ledger_path = scenes_dir / "scene_fallback.json"

    def _mark_fallback(scene_no: int, reason: str) -> None:
        try:
            led = {}
            if _fallback_ledger_path.exists():
                led = json.loads(_fallback_ledger_path.read_text(encoding="utf-8"))
            led[str(scene_no)] = {
                "reason": reason,
                "marked_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            _fallback_ledger_path.write_text(json.dumps(led, indent=2), encoding="utf-8")
            logger.warning(
                f"  ⚑ Scene {scene_no} marked FALLBACK_USED ({reason}) — "
                f"QC will refuse to call this visually complete")
        except Exception:
            pass

    try:
        _fallback_ledger_path.write_text("{}", encoding="utf-8")
    except Exception:
        pass

    visuals_cfg = template.get("visuals", {})
    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])
    clip_policy = visuals_cfg.get("clip_policy", {})
    max_clip_seconds = float(clip_policy.get("max_clip_seconds", 7.0))

    for scene in script.scenes:
        sn = scene.scene_number
        primary = scene.visual_prompt or "abstract landscape"
        alts = scene.alternative_visual_prompts or []
        vtype = scene.visual_type or "stock_video"
        mood = scene.mood or "neutral"
        people = scene.people_to_show or []

        logger.info(f"Scene {sn} | {vtype} | '{primary[:55]}'")
        if people:
            logger.info(f"  People: {people}")

        video_path = scenes_dir / f"scene_{sn}_video.mp4"
        photo_path = scenes_dir / f"scene_{sn}_photo.jpg"
        got_asset = False

        # ── FIX-050 resume guard: a scene whose primary asset is already on
        # disk skips the whole search cascade — only the b-roll pairing pass
        # still runs. (The old behavior re-searched every scene on every
        # resume and could swap a cached scene's provenance for a fresh hit.)
        _vp = Path(scene.video_path) if scene.video_path else None
        _have_photo = bool(scene.photo_path) and Path(scene.photo_path).exists() \
            and valid_photo(Path(scene.photo_path))
        _have_video = bool(_vp and _vp.exists() and _vp.stat().st_size > 0
                           and "gradient" not in _vp.name)
        if _have_photo or _have_video:
            try:
                await asyncio.wait_for(
                    _pair_broll_cutaway(scene, people, primary, scenes_dir, sn),
                    timeout=240.0,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Scene {sn}: b-roll pairing timed out — continuing without cutaway")
            continue

        # ── Priority Check: Historical/Celebrity Portraits via Wikimedia ──
        if people:
            for person in people:
                if got_asset:
                    break
                wiki_url = await search_wikimedia_portrait(person)
                if wiki_url:
                    try:
                        await download_file(wiki_url, photo_path)
                        if valid_photo(photo_path):
                            scene.photo_path = str(photo_path)
                            scene.video_path = None
                            scene.source_url = wiki_url
                            scene.source_title = f"Official Portrait: {person}"
                            got_asset = True
                            logger.info(f"  👑 Got AUTHENTIC portrait from Wikimedia: '{person}'")
                            break
                        else:
                            photo_path.unlink(missing_ok=True)
                    except Exception as w_err:
                        logger.warning(f"  Wikimedia download failed for {person}: {w_err}")
                        photo_path.unlink(missing_ok=True)

        # Build search queries: person queries first if people present, else grounded B-roll
        scene_queries = []
        if people:
            for person in people:
                scene_queries.append(f"{person} portrait photograph")
                scene_queries.append(f"{person} photo hd")
                scene_queries.append(f"{person} {primary}")
                scene_queries.append(person)
            scene_queries.append(primary)
        else:
            broll_kw = getattr(scene, "broll_keywords", []) or []
            grounded = ground_visual_query(primary, broll_kw)
            scene_queries.append(primary)
            if grounded != primary:
                scene_queries.append(grounded)
            for kw in broll_kw:
                if kw not in scene_queries:
                    scene_queries.append(kw)

        # Footage-specific query order for youtube_clip scenes: yt-dlp's top
        # result for a bare person query is usually a fan montage or slideshow,
        # not narratable footage. Lead with the scene's own visual_prompt plus
        # footage-oriented suffixes, and never spend attempts on portrait queries.
        if vtype == "youtube_clip":
            footage_queries = [primary]
            footage_queries.extend(
                f"{primary} {suffix}"
                for suffix in ("news report", "interview", "documentary")
            )
            footage_queries.extend(
                q for q in scene_queries
                if q not in footage_queries and "portrait" not in q and "photo hd" not in q
            )
            scene_queries = footage_queries

        for alt in alts:
            if alt not in scene_queries:
                scene_queries.append(alt)

        # FIX-059 celebrity variety: consecutive scenes search from a
        # different slice of the same person's results (portrait / hd /
        # event shots lead in turn instead of the same top hit).
        scene_queries = _rotate_person_queries(scene_queries, sn)
        # Per-project variety ledger (fresh read — repair passes may have
        # updated it between scenes).
        variety_state = _load_variety_state(output_dir)

        try:
            # ═══════════════════════════════════════════
            # TIER 1: Primary source by visual_type
            # ═══════════════════════════════════════════
            if not got_asset and vtype in ("web_photo", "photo_person"):
                for q in scene_queries:
                    if got_asset:
                        break
                    # Try all candidate images from search rather than abandoning on candidate 0
                    urls = await search_duckduckgo_photos(q, max_results=8)
                    for url in urls:
                        if url in _used_urls or _is_banned_url(url):
                            continue
                        try:
                            await download_file(url, photo_path)
                            if not valid_photo(photo_path):
                                photo_path.unlink(missing_ok=True)
                                continue
                            # FIX-059: reject a near-duplicate of an already-
                            # used visual of the same person (variety gate).
                            _h = _image_dhash(photo_path)
                            if _scene_visual_repeat(variety_state, people, _h) >= VARIETY_MAX_REPEATS:
                                logger.info(
                                    f"  ♻️ Skipping near-duplicate visual "
                                    f"('{_person_key(people)}' already used) — variety")
                                photo_path.unlink(missing_ok=True)
                                continue
                            _used_urls.add(url)
                            scene.photo_path = str(photo_path)
                            scene.source_url = url
                            got_asset = True
                            _register_scene_visual(output_dir, sn, people, photo_path,
                                                   source=url, visual_type=vtype)
                            logger.info(f"  ✅ Got REAL photo via DDG: '{q[:40]}'")
                            break
                        except Exception as dl_err:
                            photo_path.unlink(missing_ok=True)

            elif vtype == "youtube_clip":
                # PD-first (FIX-050): archive.org newsreels are public domain —
                # real historical footage with ZERO Content-ID exposure. Try
                # it before excerpting YouTube (6s, muted, transformed).
                archive_prov = await fetch_public_domain_footage(
                    people, primary, video_path, seed=sn)
                if archive_prov:
                    scene.video_path = str(video_path)
                    scene.source_url = archive_prov.get("url", "")
                    scene.source_title = archive_prov.get("title", "")
                    scene.source_channel = archive_prov.get("channel", "")
                    got_asset = True
                    _register_scene_visual(output_dir, sn, people, video_path,
                                           source=archive_prov.get("url", ""),
                                           visual_type=vtype)
                    logger.info("  ✅ Got PUBLIC-DOMAIN footage (archive.org) — no flag risk")
                for q in scene_queries[:3]:
                    if got_asset:
                        break
                    provenance = await download_youtube_clip(
                        q, video_path,
                        max_clip_seconds=max_clip_seconds,
                        seed=sn,
                    )
                    if provenance is not None:
                        scene.video_path = str(video_path)
                        scene.source_url = provenance.get("url") or ""
                        scene.source_title = provenance.get("title") or ""
                        scene.source_channel = provenance.get("channel") or ""
                        got_asset = True
                        _register_scene_visual(output_dir, sn, people, video_path,
                                               source=provenance.get("url") or "",
                                               visual_type=vtype)
                        logger.info(
                            f"  ✅ Got YouTube clip ({max_clip_seconds:.0f}s cap): "
                            f"'{(provenance.get('title') or q)[:40]}'"
                        )
                        break
                # yt-dlp failed → try DDG photo
                if not got_asset:
                    for q in scene_queries:
                        if got_asset:
                            break
                        url = await search_duckduckgo_unique(q)
                        if url:
                            try:
                                await download_file(url, photo_path)
                                if valid_photo(photo_path):
                                    scene.photo_path = str(photo_path)
                                    scene.source_url = url
                                    got_asset = True
                                    logger.info(f"  ✅ Got DDG photo fallback: '{q[:40]}'")
                                else:
                                    photo_path.unlink(missing_ok=True)
                            except Exception:
                                photo_path.unlink(missing_ok=True)

            elif vtype == "stock_video":
                url = await _try_video_stock(
                    scene_queries[:3], narration=scene.narration, mood=mood,
                    people=people, target_size=(width, height),
                )
                if url:
                    await download_file(url, video_path)
                    if valid_video(video_path):
                        scene.video_path = str(video_path)
                        scene.source_url = url  # recorded so QC can ban junk permanently
                        got_asset = True
                        _register_scene_visual(output_dir, sn, people, video_path,
                                               source=url, visual_type=vtype)
                    else:
                        video_path.unlink(missing_ok=True)
                        logger.warning(f"  🗑️ Stock video rejected by quality floor: {url[:60]}")

            elif vtype == "stock_photo":
                url = await _try_photo_stock(
                    scene_queries[:3], narration=scene.narration, mood=mood,
                    people=people, target_size=(width, height),
                )
                if url:
                    await download_file(url, photo_path)
                    if valid_photo(photo_path):
                        scene.photo_path = str(photo_path)
                        scene.source_url = url
                        got_asset = True
                        _register_scene_visual(output_dir, sn, people, photo_path,
                                               source=url, visual_type=vtype)
                    else:
                        photo_path.unlink(missing_ok=True)

            # ═══════════════════════════════════════════
            # TIER 2: Cross-type fallback
            # ═══════════════════════════════════════════
            if not got_asset:
                logger.warning(f"Scene {sn}: Primary failed. Cross-type fallback...")

                # Try DDG with all queries
                for q in scene_queries:
                    if got_asset:
                        break
                    url = await search_duckduckgo_unique(q)
                    if url:
                        try:
                            await download_file(url, photo_path)
                            if not valid_photo(photo_path):
                                photo_path.unlink(missing_ok=True)
                                continue
                            _h = _image_dhash(photo_path)
                            if _scene_visual_repeat(variety_state, people, _h) >= VARIETY_MAX_REPEATS:
                                logger.info(
                                    f"  ♻️ Scene {sn}: skipping near-duplicate (variety)")
                                photo_path.unlink(missing_ok=True)
                                continue
                            scene.photo_path = str(photo_path)
                            scene.source_url = url
                            got_asset = True
                            _register_scene_visual(output_dir, sn, people, photo_path,
                                                   source=url, visual_type="cross_type")
                            logger.info(f"Scene {sn}: Cross-type DDG photo: '{q[:40]}'")
                        except Exception:
                            photo_path.unlink(missing_ok=True)

                # Try stock video (quality floor applies here too — the
                # bypass here is how dark theater b-roll reached a live short)
                if not got_asset:
                    url = await _try_video_stock(
                        scene_queries[:3], narration=scene.narration, mood=mood,
                        people=people, target_size=(width, height),
                    )
                    if url:
                        await download_file(url, video_path)
                        if valid_video(video_path):
                            scene.video_path = str(video_path)
                            scene.source_url = url  # recorded so QC can ban junk permanently
                            got_asset = True
                            _register_scene_visual(output_dir, sn, people, video_path,
                                                   source=url, visual_type="cross_type")
                        else:
                            video_path.unlink(missing_ok=True)
                            logger.warning("  🗑️ Cross-type stock video rejected by quality floor")

            # ═══════════════════════════════════════════
            # TIER 3: Generic mood B-roll (rotated by past usage — Phase 2.8:
            # least-used query first so consecutive videos cycle the pool)
            # ═══════════════════════════════════════════
            if not got_asset:
                logger.warning(f"Scene {sn}: Cross-type failed. Generic B-roll...")
                generic_queries = list(GENERIC_BROLL.get(mood, ["abstract background"]))
                generic_queries.sort(key=lambda q: prior_usage.get(q.lower(), 0))
                # Photo-first policy: a generic stock VIDEO (random people in
                # a supermarket, slow-mo hands) is the cheapest-looking thing
                # we can ship. A styled still with Ken Burns motion reads
                # premium. Video is the fallback, not the default.
                url = await _try_photo_stock(
                    generic_queries, narration=scene.narration, mood=mood,
                    target_size=(width, height),
                )
                if url:
                    await download_file(url, photo_path)
                    if valid_photo(photo_path):
                        scene.photo_path = str(photo_path)
                        got_asset = True
                        _register_scene_visual(output_dir, sn, people, photo_path,
                                               source=url or "", visual_type="generic_broll")
                    else:
                        photo_path.unlink(missing_ok=True)

                if not got_asset:
                    url = await _try_video_stock(
                        generic_queries, narration=scene.narration, mood=mood,
                        target_size=(width, height),
                    )
                    if url:
                        await download_file(url, video_path)
                        if valid_video(video_path):
                            scene.video_path = str(video_path)
                            scene.source_url = url  # recorded so QC can ban junk permanently
                            got_asset = True
                        else:
                            video_path.unlink(missing_ok=True)
                            logger.warning("  🗑️ Generic stock video rejected by quality floor")

            # ═══════════════════════════════════════════
            # TIER 4: Styled fallback (absolute last resort)
            # ═══════════════════════════════════════════
            if not got_asset:
                # FIX-058: before ANY gradient, two more real-visual resorts:
                # (a) a Pexels photo with generic (not scene-locked) terms,
                # (b) a Gemini-generated contextual illustration of the
                # narration — never photoreal, never presented as real.
                # The bare gradient is now the LAST resort (network dead AND
                # Gemini down), never a silent part of the finished video.
                if not got_asset:
                    generic_q = (mood.split()[0] if mood else "dramatic") + " cinematic atmosphere background"
                    url = await _try_photo_stock(
                        [generic_q, "dark cinematic texture", "moody atmosphere landscape"],
                        narration=scene.narration, mood=mood, target_size=(width, height),
                    )
                    if url:
                        try:
                            await download_file(url, photo_path)
                            if valid_photo(photo_path):
                                scene.photo_path = str(photo_path)
                                scene.visual_type = "stock_photo"
                                got_asset = True
                                _register_scene_visual(output_dir, sn, people, photo_path,
                                                       source=url or "", visual_type="generic_cinematic")
                                logger.info(f"  ✅ Scene {sn}: generic cinematic still (fallback-prevention)")
                        except Exception:
                            photo_path.unlink(missing_ok=True)
                if not got_asset:
                    gen = await _gemini_illustration(scene.narration, mood, width, height, scenes_dir, sn)
                    if gen:
                        scene.photo_path = str(gen)
                        scene.visual_type = "stock_photo"
                        got_asset = True
                        _register_scene_visual(output_dir, sn, people, gen,
                                               source="gemini_illustration",
                                               visual_type="gemini_illustration")
                        logger.info(f"  ✅ Scene {sn}: Gemini contextual illustration (clearly illustrative)")
                if not got_asset:
                    logger.warning(f"Scene {sn}: All exhausted. Generating fallback clip.")
                    duration = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
                    gradient_path = scenes_dir / f"scene_{sn}_gradient.mp4"
                    quote = scene.narration[:80] if scene.narration else ""
                    generate_fallback_clip(gradient_path, duration, mood, quote, width, height)
                    scene.video_path = str(gradient_path)
                    _mark_fallback(sn, "tier4_gradient: all sources exhausted")

        except Exception as e:
            logger.error(f"Scene {sn}: Asset fetch error: {e}")
            if not got_asset:
                gen = await _gemini_illustration(scene.narration, mood, width, height, scenes_dir, sn)
                if gen:
                    scene.photo_path = str(gen)
                    scene.visual_type = "stock_photo"
                    got_asset = True
                    _register_scene_visual(output_dir, sn, people, gen,
                                           source="gemini_illustration",
                                           visual_type="gemini_illustration")
                    logger.info(f"  ✅ Scene {sn}: Gemini contextual illustration after error")
            if not got_asset:
                duration = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
                gradient_path = scenes_dir / f"scene_{sn}_gradient.mp4"
                generate_fallback_clip(gradient_path, duration, mood, "", width, height)
                scene.video_path = str(gradient_path)
                _mark_fallback(sn, f"tier4_gradient: fetch error {str(e)[:60]}")

        # Persist what this scene actually used so the next video rotates
        # away from it (Phase 2.8). Re-recorded entries lose their decayed
        # flag (fresh last_used wins).
        _record_used(scene, primary)

        # FIX-050: give photo scenes a paired cutaway clip so the edit
        # alternates main shot / real footage like a human cut.
        try:
            await asyncio.wait_for(
                _pair_broll_cutaway(scene, people, primary, scenes_dir, sn),
                timeout=240.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Scene {sn}: b-roll pairing timed out — continuing without cutaway")

    # Flush the ledger once per video.
    _save_used_broll()

    # ═══ Loop seam: last scene echoes the hook ═══
    # Research (retention playbook + MrBeast open loops): a Short whose
    # final frame mirrors its first reads as a seamless loop — viewers
    # rewatch without noticing, doubling watch time. The final scene's
    # photo is swapped for the hook scene's still, and its cuts rebuild
    # from it on the next render.
    try:
        hook_photo = scenes_dir / "scene_1_photo.jpg"
        last = max(script.scenes, key=lambda s: s.scene_number)
        last_photo = scenes_dir / f"scene_{last.scene_number}_photo.jpg"
        if (hook_photo.exists() and last_photo.exists()
                and last.visual_type in ("web_photo", "photo_person", "stock_photo")
                and hook_photo.read_bytes() != last_photo.read_bytes()):
            shutil.copyfile(hook_photo, last_photo)
            import glob as _glob
            for c in _glob.glob(str(scenes_dir.parent / "edit_media" / f"scene_{last.scene_number:02d}_cut*")):
                Path(c).unlink(missing_ok=True)
            logger.info(f"  🔁 Loop seam: scene {last.scene_number} re-cut from the hook still")
    except Exception as exc:
        logger.warning(f"Loop-seam pass skipped: {exc}")

    return script


async def repair_scene_assets(
    script: Script, template: dict, output_dir: Path, scene_numbers: list[int],
) -> int:
    """FIX-057: the DETECT → RE-SELECT → RE-RENDER loop.

    QC flagged specific scenes (placeholder frames / fallback ledger). This
    re-runs ONLY those scenes through the candidate-pool selection loop:
    cached assets are bypassed (selection forced), `_used_urls` preserves
    whatever the first pass chose so a different candidate wins, and the
    scene's edit-media cuts are deleted so the assembler rebuilds from the
    new asset. Returns the number of scenes that got a real (non-fallback)
    asset.
    """
    scenes_dir = output_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    fallback_ledger = scenes_dir / "scene_fallback.json"

    global _used_urls
    _used_urls = set()  # fresh run; scenes we re-select must pick anew

    by_number = {s.scene_number: s for s in script.scenes}
    repaired = 0
    for sn in scene_numbers:
        scene = by_number.get(int(sn))
        if scene is None:
            continue
        logger.info(f"  🔧 Repairing scene {sn}: re-selecting asset…")

        # Bypass the resume guard: clear cached paths + delete artifacts so
        # fetch_assets MUST search again for this scene.
        scene.photo_path = None
        scene.video_path = None
        scene.broll_video_path = None
        v = scene.visual_type or "stock_video"
        if v in ("web_photo", "photo_person", "youtube_clip"):
            scene.visual_type = "stock_photo" if v != "youtube_clip" else "stock_video"
            v = scene.visual_type
        if v == "stock_photo" and scene.photo_path:
            try:
                Path(scene.photo_path).unlink(missing_ok=True)
            except OSError:
                pass

        # Force the Tier-1 stock path: strip cache files for this scene.
        for old in scenes_dir.glob(f"scene_{sn}_*"):
            old.unlink(missing_ok=True)

        # Run ONLY this scene through the standard cascade (reuse fetch_assets
        # by temporarily shrinking the script's scene list).
        full_scenes = script.scenes
        script.scenes = [scene]
        try:
            await fetch_assets(script, template, output_dir)
        finally:
            script.scenes = full_scenes

        new_path = scene.video_path or scene.photo_path
        is_fallback = bool(new_path and "gradient" in Path(new_path).name)
        if new_path and not is_fallback:
            repaired += 1
            logger.info(f"  ✅ Scene {sn} repaired with a real asset")
        else:
            _mark_fallback_local(fallback_ledger, sn, "repair pass still fell back")
            logger.warning(f"  ⚑ Scene {sn} still on fallback after repair")

        # Invalidate edit-media cuts so the assembler re-renders this scene.
        edit_media = output_dir / "edit_media"
        if edit_media.exists():
            for old in edit_media.glob(f"scene_{sn:02d}_*"):
                old.unlink(missing_ok=True)
        temp_dir = output_dir / "_temp"
        if temp_dir.exists():
            for old in temp_dir.glob(f"scene_{sn:02d}_*"):
                old.unlink(missing_ok=True)
    return repaired


def _mark_fallback_local(ledger_path: Path, scene_no: int, reason: str) -> None:
    try:
        led = {}
        if ledger_path.exists():
            led = json.loads(ledger_path.read_text(encoding="utf-8"))
        led[str(scene_no)] = {"reason": reason,
                              "marked_at": datetime.utcnow().isoformat(timespec="seconds")}
        ledger_path.write_text(json.dumps(led, indent=2), encoding="utf-8")
    except Exception:
        pass
