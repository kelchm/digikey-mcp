"""Recapture API snapshots used by the offline test suite.

Run from the project root:
    uv run python tests/refresh_snapshots.py

Requires valid CLIENT_ID / CLIENT_SECRET in .env (hits production). Each unique
(method, url, body) is written under tests/fixtures/ using a human-readable name
derived from the request shape (see tests/_snapshots.fixture_name).

To extend coverage for a new scenario, append a `find_components(...)` call to
the SCENARIOS section below. Any new requests it makes are captured automatically.
The script wipes tests/fixtures/ at the start of each run so removed scenarios
don't leave stale files behind.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._snapshots import FIXTURES_DIR, fixture_name  # noqa: E402

import digikey_mcp_server as srv  # noqa: E402

# Wipe-and-recreate so removed scenarios don't leave stale fixtures behind.
if FIXTURES_DIR.exists():
    shutil.rmtree(FIXTURES_DIR)
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

_original_make_request = srv._make_request
_captured: dict[str, dict] = {}  # name -> request body, for collision detection


def recording_make_request(method, url, headers, data=None):
    resp = _original_make_request(method, url, headers, data)
    name = fixture_name(method, url, data)
    if name in _captured and _captured[name] != data:
        raise RuntimeError(
            f"Fixture name collision: {name!r} is produced by two different request bodies. "
            f"Extend tests/_snapshots.fixture_name() to disambiguate.\n"
            f"  first body:  {_captured[name]}\n"
            f"  second body: {data}"
        )
    _captured[name] = data
    out_path = FIXTURES_DIR / f"{name}.json"
    out_path.write_text(json.dumps(
        {"request": {"method": method, "url": url, "body": data}, "response": resp},
        indent=2,
        ensure_ascii=False,
    ))
    return resp


srv._make_request = recording_make_request

# Tools are wrapped by FastMCP; unwrap to call the underlying function directly.
find_components = srv.find_components.fn
keyword_search = srv.keyword_search.fn

# === SCENARIOS — mirror what test_parametric_search.py exercises ===
CATEGORY_ID = "58"  # Aluminum Electrolytic Capacitors

print(f"Capturing API responses to {FIXTURES_DIR}/")

print("  scenario: _get_category_name")
srv._get_category_name(CATEGORY_ID)

print("  scenario: _get_parametric_filters")
srv._get_parametric_filters(CATEGORY_ID)

print("  scenario: discrete-value match")
find_components(category_id=CATEGORY_ID, attributes={"Capacitance": "470 µF"}, limit=3)

print("  scenario: same-unit range")
find_components(
    category_id=CATEGORY_ID,
    attributes={"Capacitance": {"min": "100 µF", "max": "470 µF"}},
    limit=3,
)

print("  scenario: open-ended range (min only)")
find_components(
    category_id=CATEGORY_ID,
    attributes={"Capacitance": {"min": "220 µF"}},
    limit=5,
)

print("  scenario: keyword_search with category filter")
keyword_search(keywords="Nichicon", category_id="58", limit=3)

print("  scenario: parent category with no parametric filters (Connectors)")
PARENT_CATEGORY_ID = "20"  # Connectors, Interconnects — broad parent, returns 0 facets
srv._get_category_name(PARENT_CATEGORY_ID)
srv._get_parametric_filters(PARENT_CATEGORY_ID)

print("  scenario: cross-unit range (mF bound, µF histogram)")
find_components(
    category_id=CATEGORY_ID,
    attributes={"Capacitance": {"min": "0.5 mF", "max": "5 mF"}},
    limit=2,
)

print(f"\nCaptured {len(_captured)} unique fixtures:")
for name in sorted(_captured):
    print(f"  - {name}")
