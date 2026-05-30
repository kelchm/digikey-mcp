"""Pytest configuration: runs the offline test suite against recorded API snapshots.

Side effects at module load (must happen before `digikey_mcp_server` is imported):
- Enables DIGIKEY_OFFLINE_MODE so the server skips OAuth at startup.
- Provides dummy credentials so the env check inside the server passes.
- Replaces `digikey_mcp_server._make_request` with a function that looks up captured
  fixtures in tests/fixtures/ keyed by a hash of (method, url, body).

If a test triggers an API call that hasn't been captured, the fixture lookup raises with
a clear message. Run `uv run python tests/refresh_snapshots.py` to (re)capture.
"""
import json
import os
import sys
from pathlib import Path

# Set env BEFORE importing the server module.
os.environ["DIGIKEY_OFFLINE_MODE"] = "1"
os.environ.setdefault("CLIENT_ID", "test-client-id")
os.environ.setdefault("CLIENT_SECRET", "test-client-secret")
# Force MyLists tools to register during tests. The server gates registration on
# whether a refresh token is plausibly configured (seed env var OR cache file
# present), and we want tests to exercise the registered tool wrappers, not just
# the bare functions.
os.environ.setdefault("DIGIKEY_REFRESH_TOKEN_SEED", "test-bootstrap-seed")

# Make the project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._snapshots import FIXTURES_DIR, fixture_name  # noqa: E402


def _offline_make_request(method, url, headers, data=None):
    name = fixture_name(method, url, data)
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise RuntimeError(
            f"No captured fixture for this API call.\n"
            f"  method: {method}\n"
            f"  url:    {url}\n"
            f"  body:   {json.dumps(data, sort_keys=True) if data else 'None'}\n"
            f"  expected fixture: {path}\n"
            f"  Run: uv run python tests/refresh_snapshots.py"
        )
    return json.loads(path.read_text())["response"]


import digikey_mcp_server as _srv  # noqa: E402

_srv._make_request = _offline_make_request


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_category_cache():
    """Reset the per-process category-name cache so tests are order-independent."""
    _srv._CATEGORY_NAME_CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_user_token_state():
    """Reset MyLists user-token state between tests so cache-loading / bootstrap
    paths don't leak. The token cache file path is left alone; individual tests
    point DIGIKEY_TOKEN_CACHE at a tmp_path when they need to exercise the file."""
    _srv._USER_TOKEN_STATE.update(
        {"refresh_token": None, "access_token": None, "expires_at": 0}
    )
    yield
    _srv._USER_TOKEN_STATE.update(
        {"refresh_token": None, "access_token": None, "expires_at": 0}
    )
