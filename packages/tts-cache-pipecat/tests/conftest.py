import sys
from pathlib import Path

# fake_tts and the core's test helpers import as plain modules under --import-mode=importlib.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "tts-cache" / "tests"))
