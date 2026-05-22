"""Quick test: open Chrome with UW tabs and capture a screenshot of each."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from libs.trade_lib import open_uw_browser, capture_periscope_screenshots

open_uw_browser()

print("Waiting 10 s for pages to load...")
time.sleep(10)

results = capture_periscope_screenshots("periscope_snapshots")

if results:
    print(f"\nCaptured {len(results)} screenshot(s):")
    for slug, path in results.items():
        print(f"  {slug}: {path} ({path.stat().st_size // 1024} KB)")
else:
    print("\nNo screenshots captured.")
