import streamlit as st
import asyncio
import sys
import logging
import json
from pathlib import Path
from datetime import datetime

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from main import run_pipeline
import config

# Verify model availability with a real generation probe at app start so the
# masthead shows the model this API key can actually use (FIX-036).
try:
    config.verify_gemini_models()
except Exception:
    pass

st.set_page_config(
    page_title="GhostDirector",
    page_icon="GD",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────
# Design system
#
# Warm graphite surfaces, a single amber accent, editorial type.
# Borders are 1px, radii small, no glows or gradients — the point
# is a tool that looks operated, not decorated.
# ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,600;1,9..144,400&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

:root {
    --bg: #131412;
    --panel: #1A1B18;
    --panel-2: #202119;
    --border: rgba(236, 234, 228, 0.10);
    --border-soft: rgba(236, 234, 228, 0.06);
    --ink: #ECEAE4;
    --ink-dim: #A09D93;
    --ink-faint: #6E6C64;
    --accent: #E2A33C;
    --accent-soft: rgba(226, 163, 60, 0.12);
    --ok: #8FB98F;
    --warn: #E2A33C;
    --bad: #D2705F;
}

html, body, [class*="css"], .stApp {
    font-family: 'IBM Plex Sans', -apple-system, sans-serif;
    font-size: 14.5px;
}
.stApp {
    background: var(--bg) !important;
    color: var(--ink) !important;
}
header[data-testid="stHeader"] {
    background: transparent !important;
    height: 0 !important;
}
.stMainBlockContainer {
    padding-top: 2rem !important;
    padding-bottom: 4rem !important;
    max-width: 1240px !important;
}
h1, h2, h3, h4 { color: var(--ink) !important; }
a { color: var(--accent) !important; }
hr { border-color: var(--border-soft) !important; }

/* ── Masthead ── */
.masthead {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1.1rem;
    margin-bottom: 1.6rem;
}
.wordmark {
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: 1.7rem;
    letter-spacing: -0.01em;
    color: var(--ink);
    margin: 0;
    white-space: nowrap;
}
.wordmark em { font-style: italic; color: var(--accent); }
.masthead-meta {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.74rem;
    color: var(--ink-faint);
    text-align: right;
    line-height: 1.7;
}
.masthead-meta b { color: var(--ink-dim); font-weight: 500; }

/* ── Status strip ── */
.status-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 1.6rem;
    overflow: hidden;
}
.status-cell {
    flex: 1;
    min-width: 160px;
    padding: 0.7rem 1rem;
    border-right: 1px solid var(--border-soft);
    border-bottom: 1px solid var(--border-soft);
}
.status-cell:last-child { border-right: none; }
.status-k {
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--ink-faint);
    margin-bottom: 2px;
}
.status-v {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.82rem;
    color: var(--ink);
}
.dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:6px; vertical-align:1px; }
.dot-ok  { background: var(--ok); }
.dot-warn{ background: var(--warn); }
.dot-bad { background: var(--bad); }

/* ── Panels ── */
[data-testid="stVerticalBlockBorderWrapper"] > div:first-child {
    background: var(--panel) !important;
    border: 1px solid var(--border) !important;
    border-radius: 6px !important;
    padding: 1.3rem 1.4rem !important;
    box-shadow: none !important;
}
.panel-label {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.11em;
    color: var(--ink-faint);
    margin: 0 0 0.7rem 0;
    font-weight: 500;
}
h3 { font-family: 'Fraunces', serif; font-weight: 600; font-size: 1.12rem; letter-spacing: 0; margin-bottom: 0.4rem !important; }

/* ── Inputs ── */
.stTextArea textarea, .stTextInput input {
    background: #101110 !important;
    color: var(--ink) !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    font-size: 0.92rem !important;
}
.stTextArea textarea:focus, .stTextInput input:focus {
    border-color: var(--accent) !important;
    box-shadow: none !important;
}
div[data-baseweb="select"] > div {
    background: #101110 !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
}
div[data-baseweb="select"] * { color: var(--ink) !important; }
div[data-baseweb="popover"] * { color: var(--ink) !important; }

/* Toggle: minimal, amber when on (Streamlit renders toggles as stCheckbox;
   label children = hidden span + track div + text div) */
div[data-testid="stToggle"], label[data-testid="stCheckbox"], [data-testid="stCheckbox"] span p { color: var(--ink-dim) !important; }
[data-testid="stCheckbox"] label > div:nth-of-type(1) { background: rgba(236,234,228,0.18) !important; }
[data-testid="stCheckbox"] label[data-selected="true"] > div:nth-of-type(1) { background: var(--accent) !important; }
[data-testid="stCheckbox"] label > div:nth-of-type(1) div { background: #191712 !important; }

/* ── Buttons ── */
.stButton > button, button[kind="secondary"], button[data-testid="baseButton-secondary"] {
    background: transparent !important;
    border: 1px solid var(--border) !important;
    color: var(--ink-dim) !important;
    border-radius: 4px !important;
    font-weight: 500 !important;
    font-size: 0.84rem !important;
    padding: 0.42rem 0.9rem !important;
    transition: border-color .15s, color .15s !important;
    box-shadow: none !important;
}
.stButton > button:hover, button[kind="secondary"]:hover {
    border-color: var(--accent) !important;
    color: var(--accent) !important;
    transform: none !important;
    background: var(--accent-soft) !important;
}
button[kind="primary"], button[data-testid="baseButton-primary"] {
    background: var(--accent) !important;
    color: #191712 !important;
    border: none !important;
    border-radius: 4px !important;
    font-weight: 600 !important;
    font-size: 0.95rem !important;
    padding: 0.62rem 1.4rem !important;
    box-shadow: none !important;
    transition: background .15s !important;
}
button[kind="primary"]:hover {
    background: #F0B45A !important;
    transform: none !important;
    box-shadow: none !important;
}

/* ── Tabs: quiet underline style ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 0 !important;
    background: transparent !important;
    border: none !important;
    border-bottom: 1px solid var(--border) !important;
    border-radius: 0 !important;
    padding: 0 !important;
    margin-bottom: 1.5rem !important;
}
.stTabs [data-baseweb="tab"] {
    height: 42px !important;
    border-radius: 0 !important;
    padding: 0 4px !important;
    margin-right: 1.8rem !important;
    font-weight: 500 !important;
    font-size: 0.9rem !important;
    color: var(--ink-faint) !important;
    background: transparent !important;
    border: none !important;
    border-bottom: 1px solid transparent !important;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--ink) !important; background: transparent !important; }
.stTabs [aria-selected="true"] {
    color: var(--ink) !important;
    border-bottom: 1px solid var(--accent) !important;
    background: transparent !important;
    box-shadow: none !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none !important; }

/* ── Console log ── */
.log-box {
    background: #0D0E0C;
    border: 1px solid var(--border-soft);
    border-radius: 4px;
    padding: 12px 14px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.76rem;
    color: #B9B5A6;
    height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.6;
}
.log-box::-webkit-scrollbar { width: 5px; }
.log-box::-webkit-scrollbar-thumb { background: #2A2B24; border-radius: 3px; }

/* ── Scene / variant cards ── */
.chip {
    display: inline-block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem;
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: 3px;
    color: var(--ink-dim);
    margin-right: 6px;
}
.chip-accent { color: var(--accent); border-color: rgba(226,163,60,0.4); }
.narration {
    color: var(--ink-dim);
    font-size: 0.88rem;
    line-height: 1.55;
    margin-top: 0.5rem;
}
.verdict {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.74rem;
    padding: 3px 10px;
    border-radius: 3px;
    border: 1px solid;
}
.verdict-pass { color: var(--ok); border-color: rgba(143,185,143,0.4); }
.verdict-warn { color: var(--warn); border-color: rgba(226,163,60,0.4); }
.verdict-fail { color: var(--bad); border-color: rgba(210,112,95,0.4); }

.stCodeBlock code { color: var(--ink) !important; }
.stCaption, [data-testid="stCaptionContainer"] { color: var(--ink-faint) !important; }
.stInfo, .stWarning, .stError { border-radius: 4px !important; }
</style>
""", unsafe_allow_html=True)


# ─── Log capture ──────────────────────────────────────────────
class StreamlitLogHandler(logging.Handler):
    def __init__(self, placeholder):
        super().__init__()
        self.placeholder = placeholder
        self.logs = []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.logs.append(msg)
            recent = "\n".join(self.logs[-60:])
            self.placeholder.markdown(
                f'<div class="log-box">{recent}</div>', unsafe_allow_html=True
            )
        except Exception:
            pass


# ─── Real system facts (no invented values) ───────────────────
output_dir = PROJECT_ROOT / "output"
all_projects = []
if output_dir.exists():
    all_projects = sorted(
        [d for d in output_dir.iterdir() if d.is_dir()],
        key=lambda x: x.stat().st_ctime,
        reverse=True,
    )

try:
    from pipeline.assembler_resolve import resolve_studio_installed
    resolve_installed = resolve_studio_installed()
except Exception:
    resolve_installed = False

templates_dir = PROJECT_ROOT / "templates"
available_templates = (
    [f.stem for f in templates_dir.glob("*.json") if not f.stem.startswith("_")]
    if templates_dir.exists() else []
)
n_music = sum(
    len(list(d.glob("*.mp3")) + list(d.glob("*.wav")))
    for d in config.MUSIC_DIR.iterdir() if d.is_dir()
) if config.MUSIC_DIR.exists() else 0
n_sfx = len(list(config.SFX_DIR.glob("*.wav"))) if config.SFX_DIR.exists() else 0


def _dot(ok): return "dot-ok" if ok else "dot-warn"


# ─── Masthead ─────────────────────────────────────────────────
st.markdown(f"""
<div class="masthead">
    <h1 class="wordmark">Ghost<em>Director</em></h1>
    <div class="masthead-meta">
        <b>{len(all_projects)}</b> renders in vault &nbsp;·&nbsp; model <b>{config.GEMINI_MODEL}</b><br>
        music {n_music} tracks &nbsp;·&nbsp; sfx {n_sfx} &nbsp;·&nbsp; resolve studio {'detected' if resolve_installed else 'not found'}
    </div>
</div>
""", unsafe_allow_html=True)

# ─── Status strip ─────────────────────────────────────────────
st.markdown(f"""
<div class="status-strip">
    <div class="status-cell"><div class="status-k">Script engine</div>
        <div class="status-v"><span class="dot {_dot(bool(config.GEMINI_API_KEY))}"></span>{'Gemini connected' if config.GEMINI_API_KEY else 'no API key'}</div></div>
    <div class="status-cell"><div class="status-k">Stock footage</div>
        <div class="status-v"><span class="dot {_dot(bool(config.PEXELS_API_KEY))}"></span>{'Pexels + web' if config.PEXELS_API_KEY else 'web sources only'}</div></div>
    <div class="status-cell"><div class="status-k">Narration</div>
        <div class="status-v"><span class="dot dot-ok"></span>Edge-TTS pool</div></div>
    <div class="status-cell"><div class="status-k">DaVinci hand-off</div>
        <div class="status-v"><span class="dot {_dot(resolve_installed)}"></span>{'FCPXML + live push' if resolve_installed else 'FCPXML export'}</div></div>
    <div class="status-cell"><div class="status-k">Upload API</div>
        <div class="status-v"><span class="dot {_dot(config.YOUTUBE_CLIENT_SECRETS_FILE.exists())}"></span>{'OAuth ready' if config.YOUTUBE_CLIENT_SECRETS_FILE.exists() else 'manual upload'}</div></div>
</div>
""", unsafe_allow_html=True)

# ─── Tabs ─────────────────────────────────────────────────────
tab_studio, tab_storyboard, tab_vault, tab_publish, tab_growth, tab_system = st.tabs([
    "New video", "Storyboard", "Vault", "Publishing", "Growth", "System",
])

# ═════════════════════════════════════════════════════════════
# NEW VIDEO
# ═════════════════════════════════════════════════════════════
with tab_studio:
    left, right = st.columns([1.5, 1.3], gap="large")

    with left:
        with st.container(border=True):
            st.markdown('<p class="panel-label">Subject</p>', unsafe_allow_html=True)
            st.markdown('<h3>What is the video about?</h3>', unsafe_allow_html=True)
            st.caption("One line. A person, a fall, a fortune, a question. The researcher and scriptwriter build the rest.")

            suggestions = [
                "The Rise and Fall of Elizabeth Holmes",
                "The Collapse of Enron: Where Did the Money Go",
                "How Bernie Madoff Fooled Wall Street for Decades",
                "The Disappearance of the MH370",
            ]
            chip_cols = st.columns(2)
            for i, s in enumerate(suggestions):
                with chip_cols[i % 2]:
                    if st.button(s, key=f"sug{i}", use_container_width=True):
                        st.session_state["topic_input"] = s

            topic = st.text_area(
                "Topic",
                value=st.session_state.get("topic_input", suggestions[0]),
                height=70,
                key="topic_input",
                label_visibility="collapsed",
            )

        with st.container(border=True):
            st.markdown('<p class="panel-label">Format</p>', unsafe_allow_html=True)
            template_meta = {
                "celebrity":     "Long-form investigative, ~8 min",
                "celebrity_4min": "Mid-form, ~4 min, 22 scenes",
                "celebrity_8min": "Long-form, 44 scenes, mid-roll eligible",
                "documentary":   "Historical analysis, educational pacing",
                "short_hook":    "Vertical short, maximum CTR",
            }
            default_idx = (
                available_templates.index("celebrity_4min")
                if "celebrity_4min" in available_templates else 0
            )
            selected_template = st.selectbox(
                "Template", available_templates, index=default_idx,
                format_func=lambda x: f"{x.replace('_', ' ')} — {template_meta.get(x, 'custom')}",
                label_visibility="collapsed",
            )

            c1, c2 = st.columns(2)
            with c1:
                gen_shorts = st.toggle("Render a 9:16 short as well", value=True)
                auto_pilot = st.toggle("Full autopilot (no script pause)", value=True)
            with c2:
                tts_choice = st.selectbox(
                    "Voice", ["edge-tts (neural pool)", "elevenlabs (requires key)"]
                )
                render_choice = st.selectbox(
                    "Render", ["ffmpeg (headless)", "ffmpeg + resolve timeline"]
                )

        launch = st.button("Render video", type="primary", use_container_width=True)

    with right:
        with st.container(border=True):
            st.markdown('<p class="panel-label">Console</p>', unsafe_allow_html=True)
            log_placeholder = st.empty()
            log_placeholder.markdown(
                '<div class="log-box">Idle. Set a subject and render.</div>',
                unsafe_allow_html=True,
            )

        if launch:
            if not topic.strip():
                st.error("Give the video a subject first.")
            elif not config.GEMINI_API_KEY:
                st.error("GEMINI_API_KEY is missing from .env — script stages cannot run.")
            else:
                status = st.status("Rendering…", expanded=True)
                with status:
                    handler = StreamlitLogHandler(log_placeholder)
                    handler.setFormatter(logging.Formatter(
                        "%(asctime)s %(message)s", datefmt="%H:%M:%S"
                    ))
                    root_logger = logging.getLogger("ghostdirector")
                    root_logger.addHandler(handler)
                    try:
                        provider = "elevenlabs" if "elevenlabs" in tts_choice else "edge-tts"
                        asyncio.run(run_pipeline(
                            topic=topic.strip(),
                            template_name=selected_template,
                            tts_provider=provider,
                            render_mode="ffmpeg",
                            generate_shorts=gen_shorts,
                            review=not auto_pilot,
                            auto=auto_pilot,
                            upload=False,
                            privacy="private",
                        ))
                        status.update(label="Render complete", state="complete", expanded=False)
                        st.success("Done. The video is in the Vault tab; the edit is in resolve_project/ for DaVinci.")
                        st.rerun()
                    except Exception as err:
                        status.update(label="Render interrupted", state="error")
                        st.error(f"{err}")
                    finally:
                        root_logger.removeHandler(handler)

# ═════════════════════════════════════════════════════════════
# STORYBOARD
# ═════════════════════════════════════════════════════════════
with tab_storyboard:
    if not all_projects:
        st.info("Nothing in the vault yet. Render a video first.")
    else:
        proj_names = {
            p.name: f"{p.name.split('_202')[0].replace('-', ' ')} — {datetime.fromtimestamp(p.stat().st_ctime).strftime('%b %d, %H:%M')}"
            for p in all_projects
        }
        active = st.selectbox(
            "Project", list(proj_names.keys()),
            format_func=lambda x: proj_names[x], key="sb_proj",
        )
        sb_path = output_dir / active
        sb_script_file = sb_path / "script.json"

        if sb_script_file.exists():
            try:
                script_data = json.loads(sb_script_file.read_text(encoding="utf-8"))
                scenes = script_data.get("scenes", [])

                m1, m2, m3 = st.columns(3)
                with m1:
                    st.markdown(f"**Title** — {script_data.get('title', 'Untitled')}")
                with m2:
                    st.markdown(f"**Scenes** — {len(scenes)}")
                with m3:
                    st.markdown(f"**Runtime** — ~{script_data.get('estimated_duration_minutes', 0):.1f} min")

                for i in range(0, len(scenes), 2):
                    row = scenes[i:i + 2]
                    cols = st.columns(len(row), gap="medium")
                    for col, sc in zip(cols, row):
                        sn = sc.get("scene_number", i + 1)
                        dur = sc.get("audio_duration_seconds") or sc.get("duration_target_seconds", 5.0)
                        with col:
                            with st.container(border=True):
                                st.markdown(
                                    f'<span class="chip chip-accent">scene {sn}</span>'
                                    f'<span class="chip">{(sc.get("mood") or "").lower()}</span>'
                                    f'<span class="chip">{dur:.1f}s</span>'
                                    f'<span class="chip">{sc.get("visual_type", "")}</span>'
                                    f'<span class="chip">{sc.get("sfx_cue", "")}</span>',
                                    unsafe_allow_html=True,
                                )
                                p_path = sc.get("photo_path")
                                v_path = sc.get("video_path")
                                if p_path and Path(p_path).exists():
                                    st.image(p_path, use_container_width=True)
                                elif v_path and Path(v_path).exists():
                                    st.video(v_path)
                                else:
                                    st.caption("No media attached.")
                                a_path = sc.get("audio_path")
                                if a_path and Path(a_path).exists():
                                    st.audio(a_path)
                                st.markdown(
                                    f'<div class="narration">{sc.get("narration", "")}</div>',
                                    unsafe_allow_html=True,
                                )
                                with st.expander("Media details & replacement"):
                                    st.caption(f"Visual prompt: {sc.get('visual_prompt', '')}")
                                    if sc.get("broll_keywords"):
                                        st.caption(f"B-roll: {', '.join(sc.get('broll_keywords', []))}")
                                    if sc.get("people_to_show"):
                                        st.caption(f"People: {', '.join(sc.get('people_to_show', []))}")
                                    up = st.file_uploader(
                                        f"Replace scene {sn} media",
                                        type=["jpg", "jpeg", "png", "mp4"],
                                        key=f"asset_{sn}",
                                    )
                                    if up:
                                        ext = up.name.split(".")[-1].lower()
                                        if ext in ("jpg", "jpeg", "png"):
                                            tgt = sb_path / "scenes" / f"scene_{sn}_photo.jpg"
                                            tgt.write_bytes(up.read())
                                            sc["photo_path"] = str(tgt)
                                            sc["video_path"] = None
                                        elif ext == "mp4":
                                            tgt = sb_path / "scenes" / f"scene_{sn}_video.mp4"
                                            tgt.write_bytes(up.read())
                                            sc["video_path"] = str(tgt)
                                            sc["photo_path"] = None
                                        sb_script_file.write_text(
                                            json.dumps(script_data, indent=2), encoding="utf-8"
                                        )
                                        st.success(f"Scene {sn} updated. Re-render to see it in the video.")
            except Exception as e:
                st.error(f"Could not read storyboard: {e}")
        else:
            st.warning("No script.json in this project.")

# ═════════════════════════════════════════════════════════════
# VAULT
# ═════════════════════════════════════════════════════════════
with tab_vault:
    if not all_projects:
        st.info("Nothing in the vault yet.")
    else:
        proj_names = {
            p.name: f"{p.name.split('_202')[0].replace('-', ' ')} — {datetime.fromtimestamp(p.stat().st_ctime).strftime('%b %d, %H:%M')}"
            for p in all_projects
        }
        selected = st.selectbox(
            "Project", list(proj_names.keys()),
            format_func=lambda x: proj_names[x], key="vault_proj",
        )
        proj_path = output_dir / selected

        # Compliance verdict, if the gate ran
        comp_file = proj_path / "compliance_report.json"
        if comp_file.exists():
            try:
                rep = json.loads(comp_file.read_text(encoding="utf-8"))
                v = rep.get("verdict", "?")
                label = {"pass": "compliance: pass", "warn": "compliance: warnings", "fail": "compliance: failed"}.get(v, f"compliance: {v}")
                st.markdown(
                    f'<span class="verdict verdict-{v}">{label}</span> '
                    f'<span style="color:var(--ink-faint);font-size:0.8rem">'
                    f'{rep.get("summary", {}).get("fail", 0)} fail · {rep.get("summary", {}).get("warn", 0)} warn — details in compliance_report.json</span>',
                    unsafe_allow_html=True,
                )
            except Exception:
                pass

        vcol, mcol = st.columns([1.5, 1.3], gap="large")
        with vcol:
            st.markdown('<p class="panel-label">Screening room</p>', unsafe_allow_html=True)
            vt16, vt9, vtt = st.tabs(["16:9 master", "9:16 short", "Thumbnail"])

            def _embed_video(path: Path):
                """Streamlit st.video embeds media through the page itself — a
                full-length 1080p master (100+ MB) would stall the script run
                for minutes. Stream small files; point big ones at the file
                path / download instead."""
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb <= 48:
                    st.video(str(path))
                else:
                    st.info(
                        f"Master is {size_mb:.0f} MB — too large to stream in-page. "
                        f"Preview it in your player: `{path}`"
                    )

            with vt16:
                f16 = proj_path / "final_16x9.mp4"
                if f16.exists():
                    _embed_video(f16)
                    st.download_button("Download 16:9 master", data=open(f16, "rb"),
                                       file_name=f"{selected}_16x9.mp4", mime="video/mp4",
                                       use_container_width=True)
                else:
                    st.caption("Not rendered.")
            with vt9:
                f9 = proj_path / "final_9x16_short.mp4"
                if f9.exists():
                    c1, c2, c3 = st.columns([1, 2, 1])
                    with c2:
                        _embed_video(f9)
                    st.download_button("Download short", data=open(f9, "rb"),
                                       file_name=f"{selected}_short.mp4", mime="video/mp4",
                                       use_container_width=True)
                else:
                    st.caption("No short rendered for this project.")
            with vtt:
                th = proj_path / "thumbnail.png"
                if th.exists():
                    st.image(str(th), use_container_width=True)
                    st.download_button("Download thumbnail", data=open(th, "rb"),
                                       file_name=f"{selected}_thumb.png", mime="image/png",
                                       use_container_width=True)
                else:
                    st.caption("No thumbnail.")

        with mcol:
            st.markdown('<p class="panel-label">Package</p>', unsafe_allow_html=True)
            meta_file = proj_path / "metadata.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    st.markdown("**Title**")
                    st.code(meta.get("title", ""), language=None)
                    st.markdown("**Description & chapters**")
                    st.text_area("Description", value=meta.get("description", ""),
                                 height=230, key="vault_desc", label_visibility="collapsed")
                    if meta.get("midroll_markers"):
                        st.caption(f"Mid-roll cues: {', '.join(meta['midroll_markers'])}")
                except Exception as e:
                    st.error(f"Metadata unreadable: {e}")
            else:
                st.caption("No metadata.json in this project.")

        # DaVinci Resolve hand-off
        res_dir = proj_path / "resolve_project"
        st.markdown('<p class="panel-label">DaVinci Resolve</p>', unsafe_allow_html=True)
        if res_dir.exists():
            r1, r2, r3 = st.columns(3)
            fx = res_dir / "timeline.fcpxml"
            srt = res_dir / "captions.srt"
            edl = res_dir / "timeline.edl"
            with r1:
                if fx.exists():
                    st.markdown("**Timeline (FCPXML)**")
                    st.caption("File → Import → Timeline in Resolve Free or Studio. Imports the exact edit: every cut, VO/SFX/music lanes, markers.")
                    st.download_button("Download timeline.fcpxml", data=open(fx, "rb"),
                                       file_name="timeline.fcpxml", mime="application/xml",
                                       use_container_width=True)
            with r2:
                if srt.exists():
                    st.markdown("**Captions (SRT)**")
                    st.caption("Timeline → Import → Subtitle. Same word-beat captions as the render, fully editable.")
                    st.download_button("Download captions.srt", data=open(srt, "rb"),
                                       file_name="captions.srt", mime="text/plain",
                                       use_container_width=True)
            with r3:
                if edl.exists():
                    st.markdown("**EDL fallback**")
                    st.caption("For other NLEs. Video cuts + VO only.")
                    st.download_button("Download timeline.edl", data=open(edl, "rb"),
                                       file_name="timeline.edl", mime="text/plain",
                                       use_container_width=True)
            st.caption(
                f"Bundle location: {res_dir.resolve()} — with Resolve Studio open "
                "(External Scripting = Local) re-running the pipeline pushes this edit into Studio automatically."
            )

        # ── A9: Thumbnail style studio ──
        st.markdown('<p class="panel-label">Thumbnail style studio</p>', unsafe_allow_html=True)
        st.caption("Ranked for your niche — your templates first. 'Use' makes a style the "
                   "project thumbnail (that's the file uploads attach).")
        _v1, _v2 = st.columns([1.6, 1.2])
        with _v1:
            _all_styles = st.toggle("Render ALL styles (not just the recommended)", key="vault_all_styles")
        with _v2:
            _gen = st.button("Generate style variants", use_container_width=True)
        if _gen:
            try:
                from pipeline.thumbnail import render_style_variants
                from pipeline.thumbnail_styles import CURATED_IDS, load_user_templates
                _bg_src = proj_path / "thumbnail.png"
                if not _bg_src.exists():
                    st.error("No thumbnail.png base in this project yet.")
                else:
                    _m = {}
                    _mf = proj_path / "metadata.json"
                    if _mf.exists():
                        _m = json.loads(_mf.read_text(encoding="utf-8"))
                    _hook = (_m.get("hook_overlay_text", "") + " " + _m.get("title", "")).strip()
                    from PIL import Image as _PILImage
                    _bg = _PILImage.open(_bg_src).convert("RGB")
                    _only = None
                    if _all_styles:
                        _only = CURATED_IDS + [t["id"] for t in load_user_templates()]
                    with st.spinner("Rendering styles…"):
                        _rows = render_style_variants(
                            _bg, _hook.split()[:4] or ["WATCH"], 96,
                            _bg.size[0], _bg.size[1], proj_path,
                            hook_text=_hook, niche=config.CHANNEL_NICHE,
                            only_styles=_only,
                        )
                    st.session_state[f"style_rows_{selected}"] = _rows
                    st.rerun()
            except Exception as e:
                st.error(f"Style render failed: {e}")
        _srows = st.session_state.get(f"style_rows_{selected}", [])
        if _srows:
            for _k in range(0, len(_srows), 3):
                _chunk = _srows[_k:_k + 3]
                _cols = st.columns(3, gap="medium")
                for _c, _r in zip(_cols, _chunk):
                    _sf = proj_path / _r["file"]
                    with _c:
                        if _sf.exists():
                            st.image(str(_sf), use_container_width=True)
                            st.caption(_r.get("approach", _r["id"]))
                            if st.button("Use", key=f"use_{_r['id']}", use_container_width=True):
                                import shutil
                                shutil.copyfile(_sf, proj_path / "thumbnail.png")
                                st.success(f"'{_r['id']}' is now the project thumbnail.")

# ═════════════════════════════════════════════════════════════
# PUBLISHING
# ═════════════════════════════════════════════════════════════
with tab_publish:
    if not all_projects:
        st.info("Nothing in the vault yet.")
    else:
        proj_names = {
            p.name: f"{p.name.split('_202')[0].replace('-', ' ')} — {datetime.fromtimestamp(p.stat().st_ctime).strftime('%b %d, %H:%M')}"
            for p in all_projects
        }
        active = st.selectbox(
            "Project", list(proj_names.keys()),
            format_func=lambda x: proj_names[x], key="pub_proj",
        )
        pub_path = output_dir / active
        meta_file = pub_path / "metadata.json"
        script_file = pub_path / "script.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        script = json.loads(script_file.read_text(encoding="utf-8")) if script_file.exists() else {}

        st.markdown('<p class="panel-label">Thumbnail variants — upload all three to Studio Test & Compare</p>', unsafe_allow_html=True)
        thumb_cols = st.columns(3, gap="medium")
        thumb_files = [
            ("thumbnail.png", "Concept A — focal subject"),
            ("thumbnail_v2_split.png", "Concept B — documentary box"),
            ("thumbnail_v3_minimal.png", "Concept C — cinematic minimal"),
        ]
        for col, (fname, label) in zip(thumb_cols, thumb_files):
            t = pub_path / fname
            if not t.exists():
                t = pub_path / "thumbnail.png"
            with col:
                with st.container(border=True):
                    st.markdown(f"**{label}**")
                    if t.exists():
                        st.image(str(t), use_container_width=True)
                        st.download_button("Download", data=open(t, "rb"),
                                           file_name=f"{active}_{t.name}", mime="image/png",
                                           key=f"dl_{t.name}_{active}", use_container_width=True)
                    else:
                        st.caption("Not generated.")

        st.divider()

        st.markdown('<p class="panel-label">Title variants</p>', unsafe_allow_html=True)
        variants = meta.get("title_variants", [meta.get("title", "")])
        vcols = st.columns(min(len(variants), 3))
        for col, title in zip(vcols, variants[:3]):
            with col:
                with st.container(border=True):
                    st.code(title, language=None)

        st.divider()

        dcol, pcol = st.columns([1.6, 1.2], gap="large")
        with dcol:
            st.markdown('<p class="panel-label">Description & chapters</p>', unsafe_allow_html=True)
            st.text_area("Description", value=meta.get("description", ""), height=250,
                         key="pub_desc", label_visibility="collapsed")
        with pcol:
            st.markdown('<p class="panel-label">Pinned comment</p>', unsafe_allow_html=True)
            st.text_area("Pinned comment", value=meta.get("pinned_comment", ""), height=130,
                         key="pub_pinned", label_visibility="collapsed")
            tags = meta.get("tags", [])
            if tags:
                st.markdown('<p class="panel-label">Tags</p>', unsafe_allow_html=True)
                st.code(", ".join(tags), language=None)

        st.divider()
        st.markdown('<p class="panel-label">Publish to YouTube</p>', unsafe_allow_html=True)

        _variants = meta.get("title_variants", [meta.get("title", "")]) or [meta.get("title", "")]
        _title_idx = st.selectbox("Title to publish", range(len(_variants)),
                                  format_func=lambda i: _variants[i], key="pub_title_idx")
        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button("Save edits to metadata", use_container_width=True,
                         help="Writes the chosen title + edited description/pinned comment "
                              "into metadata.json — uploads use exactly this text."):
                meta["title"] = _variants[_title_idx]
                meta["title_variants"] = _variants
                meta["description"] = st.session_state.get("pub_desc", meta.get("description", ""))
                meta["pinned_comment"] = st.session_state.get("pub_pinned", meta.get("pinned_comment", ""))
                meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                st.success("Saved — uploads will use this text.")
        with _c2:
            _privacy = st.selectbox("Privacy", ["public", "unlisted", "private"], index=1,
                                    key="pub_privacy")

        _comp_verdict = None
        _crep = pub_path / "compliance_report.json"
        if _crep.exists():
            try:
                _comp_verdict = json.loads(_crep.read_text(encoding="utf-8")).get("verdict")
            except Exception:
                pass
        if _comp_verdict == "fail":
            st.error("Compliance: FAIL — resolve the report findings before uploading.")
        elif _comp_verdict == "warn":
            st.warning("Compliance: warnings — skim compliance_report.json; publishing is your call.")
        elif _comp_verdict:
            st.success("Compliance: pass")

        _f16 = pub_path / "final_16x9.mp4"
        _thumb = pub_path / "thumbnail.png"
        if not _f16.exists():
            st.caption("Render the 16:9 master first — nothing to upload yet.")
        if st.button("Upload to YouTube", type="primary", use_container_width=True,
                     disabled=(_comp_verdict == "fail" or not _f16.exists())):
            try:
                from pipeline.uploader import upload_video
                with st.spinner("Uploading to YouTube… (big file, takes minutes)"):
                    _vid = upload_video(project_dir=pub_path, video_path=_f16,
                                        thumbnail_path=_thumb if _thumb.exists() else None,
                                        privacy_status=_privacy)
                # Bookkeeping: project.json + packaging log (dedupe-safe)
                try:
                    _pj = {}
                    _pj_path = pub_path / "project.json"
                    if _pj_path.exists():
                        _pj = json.loads(_pj_path.read_text(encoding="utf-8"))
                        _pj["youtube_url"] = f"https://youtu.be/{_vid}"
                        _pj["status"] = "done"
                        _pj_path.write_text(json.dumps(_pj, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
                    from utils.channel_state import log_packaging
                    log_packaging(topic=_pj.get("topic", meta.get("title", "")),
                                  title_used=meta.get("title", ""),
                                  title_variants=_variants,
                                  thumbnail_variant=_thumb.name if _thumb.exists() else "thumbnail.png",
                                  youtube_url=f"https://youtu.be/{_vid}",
                                  channel="default")
                except Exception as rec_e:
                    st.caption(f"(uploaded; bookkeeping note: {rec_e})")
                st.session_state["last_upload_vid"] = _vid
                st.success(f"Live: https://youtu.be/{_vid}")
            except Exception as e:
                st.error(f"Upload failed: {e}")

        _pc_txt = st.session_state.get("pub_pinned", meta.get("pinned_comment", ""))
        if st.session_state.get("last_upload_vid") and _pc_txt:
            if st.button("Post pinned comment", use_container_width=True):
                try:
                    from pipeline.uploader import post_pinned_comment, get_authenticated_service
                    _ok = post_pinned_comment(get_authenticated_service(),
                                              st.session_state["last_upload_vid"], _pc_txt)
                    st.success("Pinned comment posted." if _ok else "Could not post — check Studio.")
                except Exception as e:
                    st.error(f"Pinned comment failed: {e}")

        # ── A/B swap on a LIVE video ──
        st.markdown('<p class="panel-label">A/B test — swap title or thumbnail on a live video</p>',
                    unsafe_allow_html=True)
        st.caption("Rotate the losing variant for the winner once Studio shows CTR. "
                   "Every swap is logged for the packaging leaderboard.")
        _ab_vid = st.text_input("Live video ID", value=st.session_state.get("last_upload_vid", ""),
                                key="ab_vid_input", placeholder="e.g. ixWHAOwzNOw")
        _ab_c1, _ab_c2, _ab_c3 = st.columns([2, 1, 1])
        with _ab_c1:
            _ab_title_idx = st.selectbox("New title", range(len(_variants)),
                                         index=_title_idx, key="ab_title_sel",
                                         format_func=lambda i: (meta.get("title_variants", [meta.get("title", "")])[i]
                                                                if meta.get("title_variants")
                                                                else (_variants[i] if i < len(_variants) else "—")))
        with _ab_c2:
            import glob as _glob
            _thumb_files = sorted(_glob.glob(str(pub_path / "thumbnail*.png")))
            _ab_thumb = st.selectbox("New thumbnail", ["(keep)"] + _thumb_files, key="ab_thumb_sel",
                                     format_func=lambda p: Path(p).name if p != "(keep)" else "(keep)")
        with _ab_c3:
            st.write("")
            _ab_go = st.button("Apply swap", type="primary", use_container_width=True)
        if _ab_go:
            if not _ab_vid.strip():
                st.error("Enter the live video ID first.")
            else:
                try:
                    from pipeline.abtest import ab_set
                    _tv = (meta.get("title_variants") or _variants)
                    _res = ab_set(_ab_vid.strip(), pub_path,
                                  title_index=_ab_title_idx if _ab_title_idx < len(_tv) else None,
                                  thumb_file=None if _ab_thumb == "(keep)" else _ab_thumb)
                    if _res.get("ok"):
                        st.success("Swapped: " + "; ".join(_res.get("changes", [])))
                    else:
                        st.error("Swap failed — see log.")
                except Exception as e:
                    st.error(f"Swap failed: {e}")

# ═════════════════════════════════════════════════════════════
# GROWTH — the learning loop (retention data → next edit)
# ═════════════════════════════════════════════════════════════
with tab_growth:
    # Deferred success messages (survive the st.rerun() pattern)
    _pending_topic = st.session_state.pop("topic_loaded_msg", None)
    if _pending_topic:
        st.success(f"'{_pending_topic}' loaded — open the New video tab and press Render.")
    _scout_msg = st.session_state.pop("scout_msg", None)
    if _scout_msg:
        st.success(_scout_msg)

    st.markdown('<p class="panel-label">Learning loop</p>', unsafe_allow_html=True)
    st.caption(
        "Retention curves joined to rendered scenes; CTR joined to packaging. "
        "Populated by `python main.py --sync-analytics` (OAuth) and CTR imports from Studio."
    )

    _analytics_file = PROJECT_ROOT / "db" / "analytics.json"
    _store = {}
    if _analytics_file.exists():
        try:
            _store = json.loads(_analytics_file.read_text(encoding="utf-8"))
        except Exception:
            _store = {}
    _videos = _store.get("videos", {}) if isinstance(_store, dict) else {}

    # ── Sync controls ──
    g1, g2 = st.columns([1, 2])
    with g1:
        if st.button("Sync analytics now", use_container_width=True):
            try:
                from pipeline.analytics import sync_channel
                res = sync_channel()
                st.success(f"{res['synced']} synced · {res['skipped']} skipped")
                st.rerun()
            except FileNotFoundError as e:
                st.error(f"OAuth not configured: {e}")
            except Exception as e:
                st.error(f"Sync failed: {e}")
    with g2:
        last = _store.get("last_sync") if isinstance(_store, dict) else None
        st.caption(
            f"Last sync: {last['at']} ({last['synced']} videos)" if last
            else "Never synced. Videos need 25+ views before retention joins appear."
        )

    # ── Retention table ──
    st.markdown('<p class="panel-label">Retention by video — worst scene flagged</p>', unsafe_allow_html=True)
    if _videos:
        rows = []
        for vid, v in _videos.items():
            worst = v.get("worst_scene")
            rows.append({
                "video": (v.get("title") or vid)[:44],
                "views": v.get("views", 0),
                "AVD (s)": v.get("average_view_duration_seconds"),
                "retention %": (round((v.get("average_view_duration_seconds") or 0)
                                      / v["length_seconds"] * 100, 1)
                                if v.get("length_seconds") else None),
                "CTR %": v.get("ctr_percent"),
                "worst scene": f"#{worst['scene']} (−{worst['drop_percent']}%)" if worst else "—",
                "subs +": v.get("subscribers_gained"),
            })
        rows.sort(key=lambda r: -r["views"])
        st.dataframe(rows, use_container_width=True, hide_index=True)

        # Worst-scene deep dive: join scene number back to the script for mood/narration
        with st.expander("Worst-scene deep dive (what to fix in the next script)"):
            for vid, v in _videos.items():
                worst = v.get("worst_scene")
                if not worst:
                    continue
                proj = v.get("project_dir")
                mood = narration = None
                if proj:
                    try:
                        sc = json.loads((Path(proj) / "script.json").read_text(encoding="utf-8"))
                        scene = sc.get("scenes", [])[worst["scene"] - 1]
                        mood = scene.get("mood")
                        narration = scene.get("narration", "")[:180]
                    except Exception:
                        pass
                st.markdown(
                    f"**{(v.get('title') or vid)[:60]}** — scene {worst['scene']} "
                    f"lost {worst['drop_percent']}% of viewers "
                    f"{f'· mood: {mood}' if mood else ''}",
                )
                if narration:
                    st.markdown(f'<div class="narration">“{narration}”</div>', unsafe_allow_html=True)
    else:
        st.info(
            "No analytics synced yet. Flow: configure OAuth (client_secrets.json) → "
            "upload → gather views → `python main.py --sync-analytics`."
        )

    # ── Packaging leaderboard ──
    st.markdown('<p class="panel-label">Packaging leaderboard — what earns the click</p>', unsafe_allow_html=True)
    try:
        from pipeline.analytics import packaging_leaderboard
        from utils import channel_state
        _state = channel_state._load("default")
        _pb = packaging_leaderboard(_store, _state.get("packaging_log", []))
        if _pb["thumbnail_ctr"]:
            for thumb, info in _pb["thumbnail_ctr"].items():
                st.markdown(
                    f'<span class="chip chip-accent">{thumb}</span>'
                    f'<span class="chip">avg CTR {info["avg_ctr"]}%</span>'
                    f'<span class="chip">{info["samples"]} sample(s)</span>',
                    unsafe_allow_html=True,
                )
        elif _pb["logged_entries"]:
            st.caption(f"{_pb['logged_entries']} videos logged, none with CTR yet — "
                       "import from Studio below once impressions accumulate.")
        else:
            st.caption("No packaging logged yet — appears after the first upload.")
    except Exception as e:
        st.caption(f"Leaderboard unavailable: {e}")

    with st.expander("Import CTR from YouTube Studio"):
        st.caption(
            "The Analytics API does not serve browse impressions/CTR — read "
            "Reach → Impressions CTR in Studio and enter it here (keeps the "
            "A/B join real instead of guessed)."
        )
        _vid = st.text_input("Video ID or youtu.be URL", key="ctr_vid")
        _ctr = st.number_input("CTR %", min_value=0.0, max_value=100.0, step=0.1, key="ctr_val")
        if st.button("Import CTR"):
            if _vid.strip():
                from pipeline.analytics import import_manual_ctr
                _res = import_manual_ctr({_vid.strip(): float(_ctr)})
                st.success(f"Imported — {_res['packaging_entries_with_ctr']} packaging "
                           "entries now carry CTR.")
            else:
                st.error("Enter a video ID first.")

    # ── Topic queue ──
    st.markdown('<p class="panel-label">Topic queue — scout outliers waiting for production</p>', unsafe_allow_html=True)
    _queue_file = PROJECT_ROOT / "db" / "topic_queue.json"
    _queue = {"topics": []}
    if _queue_file.exists():
        try:
            _queue = json.loads(_queue_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    _queued = [t for t in _queue.get("topics", []) if t.get("status") != "produced"][:8]
    if _queued:
        for t in _queued:
            st.markdown(
                f'<span class="chip chip-accent">{t.get("outlier_ratio", "?")}×</span>'
                f'<span class="chip">{t.get("views", 0):,} views</span>'
                f'<span class="chip">{(t.get("source_channel") or "?")[:28]}</span> '
                f'<b>{t.get("topic", "")}</b>',
                unsafe_allow_html=True,
            )
        _q1, _q2 = st.columns([1, 2])
        with _q1:
            if st.button("Use top topic →", type="primary", use_container_width=True,
                         help="Marks it produced and pre-fills the New video tab"):
                from pipeline.topic_scout import next_topic
                _top = next_topic(pop=True)
                if _top:
                    st.session_state["topic_input"] = _top["topic"]
                    st.session_state["topic_loaded_msg"] = _top["topic"]
                    st.rerun()
                else:
                    st.warning("Queue empty or everything already produced.")
        with _q2:
            st.caption(f"{len(_queue.get('topics', []))} topics scored overall.")
    else:
        if st.button("Run scout now", use_container_width=True,
                     help="Scores every competitor watch-list channel (1–3 min, yt-dlp, no API quota)"):
            with st.spinner("Scouting competitors…"):
                try:
                    from pipeline.topic_scout import run_scout
                    _rep = run_scout()
                    st.session_state["scout_msg"] = (
                        f"Scout done: {_rep['outliers']} outliers from "
                        f"{_rep['channels_checked']} channels — queue now holds "
                        f"{_rep['queue_size']} topics.")
                except Exception as e:
                    st.error(f"Scout failed: {e}")
            st.rerun()

    # ── Channel audit ──
    st.markdown('<p class="panel-label">Channel audit</p>', unsafe_allow_html=True)
    if st.button("Run channel audit", use_container_width=True):
        try:
            from pipeline.channel_audit import audit, render_audit_report
            with st.spinner("Auditing the channel…"):
                _a = audit()
            st.code(render_audit_report(_a), language=None)
        except Exception as e:
            st.error(f"Audit failed: {e}")

    # ── You vs competitors ──
    st.markdown('<p class="panel-label">You vs competitors</p>', unsafe_allow_html=True)
    if st.button("Compare now", use_container_width=True,
                 help="Live sub/view stats for you and every watch-list channel (YouTube API)"):
        try:
            from pipeline.channel_audit import compare_channels
            with st.spinner("Fetching live channel stats…"):
                _cmp = compare_channels()
            st.session_state["compare_rows"] = _cmp["rows"]
            st.rerun()
        except Exception as e:
            st.error(f"Compare failed: {e}")
    _cmp_rows = st.session_state.get("compare_rows")
    if _cmp_rows:
        _table = []
        for r in _cmp_rows:
            _table.append({
                "channel": r.get("name", "?"),
                "subs": r.get("subscribers", "—"),
                "views/video": r.get("views_per_video", "—"),
                "views/sub": r.get("views_per_sub", "—"),
            })
        st.dataframe(_table, use_container_width=True, hide_index=True)
        st.caption("Views/video is the fairness metric — it ignores channel age. "
                   "Beat the niche average and the algorithm is working for you.")

    # ── Competitor discovery ──
    st.markdown('<p class="panel-label">Find competitors</p>', unsafe_allow_html=True)
    st.text_input("Niche", value=config.CHANNEL_NICHE, key="disc_niche",
                  label_visibility="collapsed")
    if st.button("Search channels for this niche", use_container_width=True):
        try:
            from pipeline.channel_audit import suggest_channels
            with st.spinner("Searching YouTube…"):
                _disc = suggest_channels(niche=st.session_state.get("disc_niche"))
            for _e in _disc["errors"]:
                st.caption(f"search failed: {_e}")
            if _disc["candidates"]:
                st.session_state["disc_candidates"] = _disc["candidates"]
                st.rerun()
            else:
                st.info("No candidates found — try a broader niche.")
        except Exception as e:
            st.error(f"Discovery failed: {e}")
    for _i, _cand in enumerate(st.session_state.get("disc_candidates", [])):
        _d1, _d2, _d3 = st.columns([2.4, 1, 0.8])
        with _d1:
            st.markdown(f"**{_cand['name']}**")
            st.caption(_cand["url"])
        with _d2:
            st.markdown(f"{_cand['subscribers']:,} subs")
        with _d3:
            if st.button("Track", key=f"track_{_cand.get('channel_id', _i)}", use_container_width=True):
                from pipeline.topic_scout import add_competitor
                add_competitor(_cand["url"])
                st.success(f"{_cand['name']} added to the watch-list.")

# ═════════════════════════════════════════════════════════════
# SYSTEM
# ═════════════════════════════════════════════════════════════
with tab_system:
    s1, s2 = st.columns(2, gap="large")

    with s1:
        with st.container(border=True):
            st.markdown('<p class="panel-label">Connections</p>', unsafe_allow_html=True)
            st.markdown(f"""
- Gemini (scripts, research): {'connected' if config.GEMINI_API_KEY else '**missing** — set GEMINI_API_KEY in .env'}
- Pexels (stock footage): {'connected' if config.PEXELS_API_KEY else 'not set — web sources still work'}
- Pixabay (extra stock): {'connected' if config.PIXABAY_API_KEY else 'optional, not set'}
- ElevenLabs (premium voice): {'connected' if config.ELEVENLABS_API_KEY else 'optional, not set'}
- YouTube upload (OAuth): {'client_secrets.json present' if config.YOUTUBE_CLIENT_SECRETS_FILE.exists() else '**not configured** — needed for --upload'}
- DaVinci Resolve Studio: {'installed — auto-push available' if resolve_installed else 'not found — FCPXML export only'}
""")
            st.caption("Fact-anchor validation, the compliance gate and the Resolve export run on every render regardless of keys.")

    with s2:
        with st.container(border=True):
            st.markdown('<p class="panel-label">Media library</p>', unsafe_allow_html=True)
            if config.MUSIC_DIR.exists():
                for mood_dir in sorted(config.MUSIC_DIR.iterdir()):
                    if mood_dir.is_dir():
                        tracks = list(mood_dir.glob("*.mp3")) + list(mood_dir.glob("*.wav"))
                        st.markdown(f"**{mood_dir.name}** — {len(tracks)} track(s)")
            else:
                st.warning("Music directory missing.")
            st.caption(f"SFX pool: {n_sfx} file(s) in assets/sfx. More files = more variety; the renderer picks them up automatically.")

        with st.container(border=True):
            st.markdown('<p class="panel-label">Voice check</p>', unsafe_allow_html=True)
            test_phrase = st.text_input(
                "Phrase", value="The story you are about to hear was buried for thirty years.",
                key="tts_test",
            )
            if st.button("Synthesize sample"):
                with st.spinner("Generating…"):
                    test_out = PROJECT_ROOT / "_temp_voice_test.mp3"
                    import edge_tts
                    async def _t():
                        await edge_tts.Communicate(test_phrase, "en-US-GuyNeural").save(str(test_out))
                    asyncio.run(_t())
                    if test_out.exists():
                        st.audio(str(test_out))
