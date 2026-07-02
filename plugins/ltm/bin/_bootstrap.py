"""Put the plugin root on sys.path so ``import core`` works from any entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    root = Path(env) if env else Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
