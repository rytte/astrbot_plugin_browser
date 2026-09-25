import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))
ASTRBOT_DIR = PLUGIN_DIR.parent / "AstrBot"
if ASTRBOT_DIR.is_dir():
    sys.path.insert(0, str(ASTRBOT_DIR))
