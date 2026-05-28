"""Shared snapshot helpers used by both conftest.py (offline test path) and
refresh_snapshots.py (online capture path). Kept side-effect-free so the refresh
script can import without triggering offline mode.

Fixture filenames are derived structurally from the request shape — same (method,
url, body) always maps to the same filename, but the name describes WHAT the call
is rather than being an opaque hash. Two semantically different requests cannot
collide because the encoding includes all distinguishing fields (CategoryId,
ParameterId, ValueIds for single-value filters, value count for ranges).
"""
import re
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _slug(s: str, maxlen: int = 24) -> str:
    """ASCII-safe filename fragment. 'µF' -> 'uf', '0.5 mF' -> '0_5_mf'."""
    s = str(s).lower().replace("µ", "u").replace("μ", "u").replace("ω", "ohm")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:maxlen]


def fixture_name(method: str, url: str, body) -> str:
    """Stable, human-readable filename (without extension) for an API call."""
    body = body or {}

    m = re.search(r"/categories/(\d+)/?$", url)
    if m:
        return f"categories_{m.group(1)}"

    if url.endswith("/search/keyword"):
        fo = body.get("FilterOptionsRequest") or {}
        cat = (fo.get("CategoryFilter") or [{}])[0].get("Id", "any")
        kw_slug = _slug(body.get("Keywords") or "any", 30)

        pf = fo.get("ParameterFilterRequest")
        if not pf:
            return f"keyword_cat{cat}_kw_{kw_slug}"

        parts = []
        for entry in sorted(pf.get("ParameterFilters") or [], key=lambda x: x.get("ParameterId", 0)):
            pid = entry.get("ParameterId")
            value_ids = [fv.get("Id") for fv in (entry.get("FilterValues") or [])]
            if not value_ids:
                continue
            if len(value_ids) == 1:
                parts.append(f"p{pid}_{_slug(value_ids[0])}")
            else:
                # Range / multi-value: include count and the bounds for searchability.
                bounds = f"{_slug(value_ids[0])}_to_{_slug(value_ids[-1])}"
                parts.append(f"p{pid}_x{len(value_ids)}_{bounds}")
        return f"keyword_cat{cat}_kw_{kw_slug}_" + "_".join(parts)

    # Fallback for any future endpoint.
    safe = re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[-40:]
    return f"{method.lower()}_{safe}"
