#!/usr/bin/env python3
"""Read-only backup of the G502 HERO onboard profile sectors.

Usage:
    python tools/dump_profiles.py [output_dir]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from g502_switcher import main  # noqa: E402

out_dir = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "backup")
raise SystemExit(main(["--dump-profiles", out_dir, "--verbose"]))
