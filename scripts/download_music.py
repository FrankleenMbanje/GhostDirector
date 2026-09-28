"""
GhostDirector — Background Music Downloader

Downloads royalty-free music from Pixabay based on moods defined in the templates.
Requires PIXABAY_API_KEY in .env.
"""

import sys
import asyncio
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from utils.logger import get_logger

log = get_logger("music_downloader")

MOODS = ["dramatic", "suspenseful", "dark", "emotional", "upbeat", "neutral", "triumphant"]

async def download_track(client: httpx.AsyncClient, url: str, dest: Path):
    if dest.exists():
        log.info(f"Already downloaded: {dest.name}")
        return
    log.info(f"Downloading {dest.name}...")
    try:
        response = await client.get(url, follow_redirects=True, timeout=60.0)
        response.raise_for_status()
        dest.write_bytes(response.content)
    except Exception as e:
        log.error(f"Failed to download {dest.name}: {e}")

async def main():
    if not config.PIXABAY_API_KEY:
        log.error("PIXABAY_API_KEY is not set in .env. Cannot download music.")
        sys.exit(1)

    log.info("Starting music download from Pixabay...")
    config.MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        for mood in MOODS:
            mood_dir = config.MUSIC_DIR / mood
            mood_dir.mkdir(parents=True, exist_ok=True)
            
            # If we already have a few tracks, skip
            existing = list(mood_dir.glob("*.mp3"))
            if len(existing) >= 3:
                log.info(f"Mood '{mood}' already has {len(existing)} tracks. Skipping.")
                continue

            log.info(f"Searching for '{mood}' music...")
            url = f"https://pixabay.com/api/audio/?key={config.PIXABAY_API_KEY}&q={mood}&per_page=10"
            try:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
                
                hits = data.get("hits", [])
                if not hits:
                    log.warning(f"No music found for mood '{mood}'.")
                    continue
                
                # Download top 3 tracks for this mood
                for i, hit in enumerate(hits[:3]):
                    download_url = hit.get("audio", "")
                    if not download_url:
                        continue
                    
                    track_name = f"{mood}_track_{i+1}.mp3"
                    dest = mood_dir / track_name
                    await download_track(client, download_url, dest)
            except Exception as e:
                log.error(f"Failed to fetch music for mood '{mood}': {e}")

if __name__ == "__main__":
    asyncio.run(main())
