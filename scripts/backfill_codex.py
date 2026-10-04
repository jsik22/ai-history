#!/usr/bin/env python3
"""Dry-run by default; --apply appends missing Codex turns idempotently."""
import runpy
import sys
from pathlib import Path
sys.argv.insert(1, '--backfill')
runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/ai-history-codex'), run_name='__main__')
