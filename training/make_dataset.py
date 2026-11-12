"""Create the versioned dataset (sklearn breast-cancer, binary target).

v1 = first 400 rows (older snapshot), v2 = all 569 rows (newer snapshot).
Usage: python training/make_dataset.py v1|v2
"""

import sys
from pathlib import Path

from sklearn.datasets import load_breast_cancer

ROWS = {"v1": 400, "v2": None}

version = sys.argv[1]
df = load_breast_cancer(as_frame=True).frame
df = df.head(ROWS[version]) if ROWS[version] else df
out = Path(__file__).resolve().parents[1] / "data" / "dataset.csv"
out.parent.mkdir(exist_ok=True)
df.to_csv(out, index=False)
print(f"{version}: wrote {len(df)} rows to {out}")
