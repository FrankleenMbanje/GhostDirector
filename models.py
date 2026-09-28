"""
GhostDirector — Shared Data Models

All data structures used across pipeline modules. Every module reads and writes
these dataclasses so the pipeline stays loosely coupled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from enum import Enum


class VisualType(str, Enum):
    STOCK_VIDEO = "stock_video"
    WEB_PHOTO = "web_photo"
    PHOTO_PERSON = "photo_person"
    STOCK_PHOTO = "stock_photo"
    YOUTUBE_CLIP = "youtube_clip"


class RenderMode(str, Enum):
    FFMPEG = "ffmpeg"
    RESOLVE = "resolve"


class TTSProvider(str, Enum):
    EDGE_TTS = "edge-tts"
    ELEVENLABS = "elevenlabs"


@dataclass
class ResearchResult:
    """Output of the researcher module."""
    topic: str
    summary: str
    timeline: list[dict]          # [{"date": "1990", "event": "..."}]
    key_people: list[dict]        # [{"name": "...", "role": "...", "search_query": "..."}]
    key_facts: list[str]
    narrative_arc: str            # Brief description of the story arc
    source_queries: list[str]     # Search queries used

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ResearchResult:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


@dataclass
class Scene:
    """A single scene in the video script."""
    scene_number: int
    narration: str
    visual_prompt: str
    visual_type: str              # "stock_video", "web_photo", "stock_photo", "youtube_clip"
    mood: str
    alternative_visual_prompts: list[str] = field(default_factory=list)
    people_to_show: list[str] = field(default_factory=list)
    duration_target_seconds: float = 7.0
    sfx_cue: Optional[str] = "whoosh"   # "whoosh", "sub_impact", "camera_shutter", "tension_riser", "none"
    broll_keywords: list[str] = field(default_factory=list)

    # Populated by later pipeline stages
    audio_path: Optional[str] = None
    audio_duration_seconds: Optional[float] = None
    video_path: Optional[str] = None
    photo_path: Optional[str] = None
    broll_video_path: Optional[str] = None   # paired cutaway footage (PD archive / stock video)
    timestamps: Optional[list[dict]] = None  # [{"word": "...", "start": 0.0, "end": 0.2}]

    # Source provenance (copyright policy: credits + dispute evidence)
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    source_channel: Optional[str] = None


@dataclass
class Script:
    """Output of the scriptwriter module. The full video script."""
    title: str
    description: str
    tags: list[str]
    hook: str
    scenes: list[Scene]
    total_scenes: int
    estimated_duration_minutes: float
    loop_phrase: Optional[str] = None
    chapters: list[dict] = field(default_factory=list)  # [{"time": "00:00", "title": "Hook"}]
    pinned_comment: Optional[str] = None
    hook_overlay_text: Optional[str] = None             # 5-7 word complementary visual title card for silent autoplay
    title_variants: list[dict] = field(default_factory=list)  # [{"type": "curiosity_gap", "title": "..."}, ...]
    midpoint_trigger: Optional[str] = None              # Revelation / stakes-raise at 50% mark to kill mid-video sag
    bonus_payoff: Optional[str] = None                  # Surprise takeaway before CTA to drive subscriptions
    midroll_markers: list[str] = field(default_factory=list)  # Suggested commercial ad break timestamps (e.g. ["03:30", "07:00"])
    anchor_report: Optional[dict] = None                # Phase 2.6 fact-anchor validation report (operator-facing)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Script:
        data = json.loads(path.read_text(encoding="utf-8"))
        scenes_data = data.pop("scenes", [])
        scenes = []
        for s in scenes_data:
            # Filter to known fields of Scene
            valid_fields = Scene.__dataclass_fields__.keys()
            filtered_s = {k: v for k, v in s.items() if k in valid_fields}
            scenes.append(Scene(**filtered_s))
        
        valid_script_fields = cls.__dataclass_fields__.keys()
        filtered_data = {k: v for k, v in data.items() if k in valid_script_fields}
        return cls(scenes=scenes, **filtered_data)


@dataclass
class VideoProject:
    """
    Master project state. Tracks the entire pipeline for one video.
    Passed between modules and persisted to disk.
    """
    topic: str
    template_name: str
    render_mode: str = "ffmpeg"       # "ffmpeg" or "resolve"
    tts_provider: str = "edge-tts"    # "edge-tts" or "elevenlabs"
    generate_shorts: bool = False

    # Paths (populated during pipeline)
    project_dir: Optional[str] = None
    research_path: Optional[str] = None
    script_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    metadata_path: Optional[str] = None
    final_video_path: Optional[str] = None
    final_shorts_path: Optional[str] = None
    youtube_url: Optional[str] = None

    # State
    research: Optional[ResearchResult] = None
    script: Optional[Script] = None
    status: str = "initialized"       # initialized → researching → scripting → voicing → ...

    def save(self, path: Path) -> None:
        """Save project state (without heavy nested objects — those have their own files)."""
        data = {
            "topic": self.topic,
            "template_name": self.template_name,
            "render_mode": self.render_mode,
            "tts_provider": self.tts_provider,
            "generate_shorts": self.generate_shorts,
            "project_dir": self.project_dir,
            "research_path": self.research_path,
            "script_path": self.script_path,
            "thumbnail_path": self.thumbnail_path,
            "metadata_path": self.metadata_path,
            "final_video_path": self.final_video_path,
            "final_shorts_path": self.final_shorts_path,
            "youtube_url": self.youtube_url,
            "status": self.status,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> VideoProject:
        data = json.loads(path.read_text(encoding="utf-8"))
        proj = cls(
            topic=data["topic"],
            template_name=data["template_name"],
        )
        for key, val in data.items():
            if hasattr(proj, key):
                setattr(proj, key, val)
        # Reload nested objects if their files exist
        if proj.research_path and Path(proj.research_path).exists():
            proj.research = ResearchResult.load(Path(proj.research_path))
        if proj.script_path and Path(proj.script_path).exists():
            proj.script = Script.load(Path(proj.script_path))
        return proj
