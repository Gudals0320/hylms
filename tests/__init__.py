"""Synthetic configuration only; no local credentials or runtime state."""
import os
from pathlib import Path
os.environ["HYLMS_CONFIG"] = str(Path(__file__).parent / "fixtures/config.json")
