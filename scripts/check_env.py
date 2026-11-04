"""Pre-flight check: verifies python, ffmpeg, ffprobe. Exit 0 = ready to build/run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.envcheck import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
