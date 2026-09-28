"""
GhostDirector — Logger

Consistent logging across all modules. Forces UTF-8 on Windows
to prevent the Rich/CP1252 crash that killed every previous run.
"""

import logging
import sys
import io

# ── Force UTF-8 on Windows consoles ──
# This is the #1 crash fix. Without this, Rich crashes on every emoji/arrow.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Now it's safe to import Rich
from rich.logging import RichHandler
from rich.console import Console

# Create a console that explicitly uses UTF-8
_console = Console(force_terminal=True, color_system="auto")


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Get a logger configured with Rich formatting.
    Safe on Windows CP1252 consoles.
    """
    logger = logging.getLogger(f"ghostdirector.{name}")

    if not logger.handlers:
        handler = RichHandler(
            console=_console,
            show_time=True,
            show_path=False,
            markup=True,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
        )
        handler.setLevel(level)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)

    return logger
