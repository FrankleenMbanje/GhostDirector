"""
Studio launcher — forces the Windows SelectorEventLoop before Streamlit starts.

Python 3.14's proactor loop on Windows raises `OSError: [WinError 64] The
specified network name is no longer available` inside the accept handler when
a client aborts mid-handshake (preview webview probes do this), which tears
down the whole server. The selector loop handles aborted connects gracefully.

Run: venv/Scripts/python.exe scripts/run_studio.py [port]
"""
import asyncio
import sys
from pathlib import Path

# ── Force the selector loop BEFORE anything imports asyncio users ──
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass

port = sys.argv[1] if len(sys.argv) > 1 else "8501"
app = str(Path(__file__).parent.parent / "app.py")

from streamlit.web import cli as stcli  # noqa: E402  (after the policy switch)

sys.argv = [
    "streamlit", "run", app,
    "--server.port", port,
    "--server.headless", "true",
    "--global.developmentMode", "false",
]
sys.exit(stcli.main())
