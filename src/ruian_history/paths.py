"""Where the pipeline keeps its data. The data directory is `$RUIAN_DATA_DIR`
if that is set, otherwise `data/` in the current working directory, so an
installed package never writes into its own installation folder."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("RUIAN_DATA_DIR", "data")).resolve()

HISTORY_DIR = DATA_DIR / "history"              # per-municipality history state (build_region)
OUTPUT_DIR = DATA_DIR / "output"                # exports of build_region
CLEAN_OUTPUT_DIR = DATA_DIR / "clean_output"    # cleaned exports
CLEANING_LOG_DIR = DATA_DIR / "cleaning_logs"   # logs of the cleaning steps
