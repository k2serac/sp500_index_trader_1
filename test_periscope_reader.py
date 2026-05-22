"""Quick test: run PeriscopeReader against the existing snapshot files."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from libs.periscope_lib import PeriscopeReader

snapshots_dir = Path("periscope_snapshots")
shots = {p.stem.split("_", 2)[-1]: p for p in sorted(snapshots_dir.glob("20260522_0219_*.png"))}

print("Screenshots found:")
for slug, path in shots.items():
    print(f"  {slug}: {path.name}")

reader = PeriscopeReader()
data = reader.read(shots)

if data:
    print("\nExtracted PeriscopeData:")
    print(data.summary())
    print(f"\nAll GEX levels for SignalEvaluator: {data.all_gex_levels()}")
else:
    print("\nFailed to extract data.")
