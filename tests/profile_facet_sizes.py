"""One-off profiler: how big are parametric facet payloads across categories?

Run from the project root with live API credentials:
    uv run python tests/profile_facet_sizes.py

Hits a handful of representative categories, measures serialized byte size of each
parameter's FilterValues histogram, and prints rankings so we can pick a sane default
cap on get_parametric_filters drill-in.
"""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import digikey_mcp_server as srv  # noqa: E402

# Hand-picked diverse leaf categories. Comments are the human names — id is what counts.
CATEGORY_IDS = [
    "58",    # Aluminum Electrolytic Capacitors
    "60",    # Ceramic Capacitors (likely huge)
    "52",    # Resistors / Chip Resistor - Surface Mount
    "78",    # LED Indication - Discrete
    "82",    # Single Diodes
    "75",    # Single Transistors (BJT)
    "20",    # Connectors
    "32",    # Inductors / Fixed Inductors
    "70",    # Crystals
    "165",   # Logic - Buffers, Drivers (just to test an IC subcat)
]


def probe(cat_id):
    name = srv._get_category_name(cat_id)
    filters = srv._get_parametric_filters(cat_id)
    rows = []
    for p in filters:
        vals = p.get("FilterValues") or []
        serialized = json.dumps(vals, ensure_ascii=False)
        rows.append({
            "category_id": cat_id,
            "category": name,
            "parameter": p.get("ParameterName"),
            "type": p.get("ParameterType"),
            "value_count": len(vals),
            "bytes": len(serialized.encode("utf-8")),
        })
    return name, rows


def main():
    all_rows = []
    print(f"Probing {len(CATEGORY_IDS)} categories...\n")
    for cid in CATEGORY_IDS:
        try:
            name, rows = probe(cid)
            print(f"  {cid:>4}  {name}: {len(rows)} parameters")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {cid:>4}  FAIL: {e}")

    if not all_rows:
        return

    print("\n=== TOP 25 PARAMETERS BY SERIALIZED BYTES ===")
    print(f"{'KB':>8}  {'values':>7}  {'type':<20}  category / parameter")
    for r in sorted(all_rows, key=lambda r: -r["bytes"])[:25]:
        print(f"  {r['bytes']/1024:>6.1f}  {r['value_count']:>7}  {r['type']:<20}  {r['category']!r} / {r['parameter']!r}")

    print("\n=== DISTRIBUTION OF value_count ACROSS ALL PARAMETERS ===")
    counts = sorted(r["value_count"] for r in all_rows)
    bytes_ = sorted(r["bytes"] for r in all_rows)
    print(f"  total params probed: {len(counts)}")
    print(f"  value_count: min={counts[0]}  p50={statistics.median(counts):.0f}"
          f"  p90={counts[int(len(counts)*0.9)]}  p99={counts[int(len(counts)*0.99)]}  max={counts[-1]}")
    print(f"  bytes (KB):  min={bytes_[0]/1024:.1f}  p50={bytes_[len(bytes_)//2]/1024:.1f}"
          f"  p90={bytes_[int(len(bytes_)*0.9)]/1024:.1f}  p99={bytes_[int(len(bytes_)*0.99)]/1024:.1f}  max={bytes_[-1]/1024:.1f}")

    print("\n=== PER-CATEGORY TOTAL FACET SIZE (if get_parametric_filters returned full data) ===")
    by_cat = {}
    for r in all_rows:
        by_cat.setdefault(r["category"], 0)
        by_cat[r["category"]] += r["bytes"]
    for cat, total in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        print(f"  {total/1024:>7.1f} KB  {cat}")

    print("\n=== HOW MANY PARAMETERS FIT VARIOUS CAPS ===")
    for cap_kb in [5, 10, 20, 50, 80]:
        cap = cap_kb * 1024
        fit = sum(1 for r in all_rows if r["bytes"] <= cap)
        print(f"  ≤{cap_kb:>3} KB  {fit:>4} / {len(all_rows)} parameters fit ({fit*100/len(all_rows):.0f}%)")


if __name__ == "__main__":
    main()
