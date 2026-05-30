import os
import re
import json
import time
import stat
import difflib
import logging
import threading
import urllib.parse
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv
import requests
import pint

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
USE_SANDBOX = os.getenv("USE_SANDBOX", "false").lower() in ("true", "1", "yes")
# Offline mode: skip OAuth at import. Tests set this to use captured fixtures via a
# patched _make_request; nothing else in the module should hit the network.
OFFLINE_MODE = os.getenv("DIGIKEY_OFFLINE_MODE", "false").lower() in ("true", "1", "yes")

# DigiKey OAuth2 token endpoint
if USE_SANDBOX:
    TOKEN_URL = "https://sandbox-api.digikey.com/v1/oauth2/token"
    AUTHORIZE_URL = "https://sandbox-api.digikey.com/v1/oauth2/authorize"
    API_BASE = "https://sandbox-api.digikey.com"
else:
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    AUTHORIZE_URL = "https://api.digikey.com/v1/oauth2/authorize"
    API_BASE = "https://api.digikey.com"

# MyLists v1 (user-context). Same host, different base path.
MYLISTS_BASE = f"{API_BASE}/mylists/v1"

# User-scoped OAuth state. None of these are read at import time; the user token is
# fetched lazily on the first MyLists tool call so the offline tests and Product
# Search tools keep working without user-auth set up.
DIGIKEY_ACCOUNT_ID = os.getenv("DIGIKEY_ACCOUNT_ID")
DIGIKEY_REDIRECT_URI = os.getenv("DIGIKEY_REDIRECT_URI", "https://localhost")
# Seed: a refresh token obtained out-of-band via `digikey-mcp-auth login`. Used ONLY
# to populate an empty cache on first MyLists call — after that the cache file is the
# source of truth (DigiKey rotates refresh tokens on every refresh, so the env-var
# value goes stale immediately).
DIGIKEY_REFRESH_TOKEN_SEED = os.getenv("DIGIKEY_REFRESH_TOKEN_SEED")


def _default_token_cache_path() -> Path:
    """XDG-style default for the per-deployment token cache."""
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "digikey-mcp" / "tokens.json"


DIGIKEY_TOKEN_CACHE = Path(os.getenv("DIGIKEY_TOKEN_CACHE") or _default_token_cache_path())

# Per-request timeout for every HTTP call to DigiKey. 30 s is generous for normal
# operation (most calls complete in 1-2 s) but ensures a stuck endpoint can't
# wedge the MCP server indefinitely — a tool call returning an exception is far
# easier to recover from than a hung process.
_REQUEST_TIMEOUT_SECS = int(os.getenv("DIGIKEY_HTTP_TIMEOUT_SECS", "30"))

# Initialize FastMCP server
mcp = FastMCP("DigiKey MCP Server")

def get_access_token():
    """Get OAuth2 access token from DigiKey."""
    # Check if credentials are loaded
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("CLIENT_ID and CLIENT_SECRET must be set in .env file")
    
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    endpoint = "SANDBOX" if USE_SANDBOX else "PRODUCTION"
    logger.info(f"Requesting token from {endpoint} with CLIENT_ID: {CLIENT_ID[:10]}...")
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=_REQUEST_TIMEOUT_SECS)
    
    if resp.status_code != 200:
        logger.error(f"OAuth error: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
    
    logger.info("Successfully obtained access token")
    return resp.json()["access_token"]

# Get access token at startup
logger.info("=== STARTING DIGIKEY MCP SERVER ===")
if USE_SANDBOX:
    logger.warning(
        "USE_SANDBOX=true — DigiKey's sandbox Product Search returns the same canned "
        "example product regardless of the query. Use it for connectivity testing only; "
        "switch to production to validate actual search behavior."
    )
if OFFLINE_MODE:
    logger.info("DIGIKEY_OFFLINE_MODE=1 — skipping OAuth; HTTP calls must be intercepted by the caller.")
    access_token = "offline-mode-no-token"
else:
    access_token = get_access_token()
logger.info("=== SERVER READY ===")

def _get_headers(customer_id: str = "0"):
    """Get standard headers for DigiKey API requests."""
    return {
        "Authorization": f"Bearer {access_token}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "Content-Type": "application/json",
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "X-DIGIKEY-Customer-Id": customer_id,
    }

def _make_request(method: str, url: str, headers: dict, data: dict = None) -> dict:
    """Make an API request with error handling and logging."""
    logger.info(f"Making {method} request to {url}")
    logger.debug(f"Headers: {json.dumps({k: v for k, v in headers.items() if 'Authorization' not in k}, indent=2)}")
    if data:
        logger.debug(f"Request body: {json.dumps(data, indent=2)}")
    
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECS)
    else:
        resp = requests.post(url, headers=headers, json=data, timeout=_REQUEST_TIMEOUT_SECS)
    
    logger.info(f"Response status: {resp.status_code}")
    if resp.status_code != 200:
        logger.error(f"API error: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
    
    return resp.json()

# --- User-context (3-legged OAuth) state for MyLists v1 -------------------------
#
# Lazily initialized on first MyLists call. DigiKey's auth-code grant returns a
# refresh_token that doesn't expire but IS rotated on every access-token refresh —
# the old refresh_token becomes invalid the moment a new one is issued. That makes
# env-vars a bad source of truth (they'd go stale after the first refresh), so the
# server persists tokens to a writable JSON cache file and reads/writes there.
#
# Bootstrap: when the cache file is missing and DIGIKEY_REFRESH_TOKEN_SEED is set,
# the server creates the cache from the seed on first use. After that, the seed is
# ignored. This lets a deployment ship a one-shot env-var secret without giving up
# rotation.
#
# In-memory fallback: if the cache file can't be written (read-only rootfs, no
# volume mounted), the server keeps the tokens in memory for the life of the
# process and logs a warning. Restart requires a fresh seed.

_USER_TOKEN_LOCK = threading.Lock()
_USER_TOKEN_STATE: dict = {
    "refresh_token": None,
    "access_token": None,
    "expires_at": 0,  # unix ts; 0 = never fetched / always-expired sentinel
}
# Refresh ~60s before the token actually expires so concurrent calls don't race
# DigiKey's clock skew. Access tokens live ~1799s, so this is a small fraction.
_TOKEN_REFRESH_LEEWAY_SECS = 60
_TOKEN_CACHE_WRITE_OK = True  # flips to False after first write failure; suppresses repeat warnings


def _read_token_cache() -> dict | None:
    """Return the cached token dict, or None if the file doesn't exist / is unreadable.

    Bad cache files raise, since silently treating them as 'no cache' would mask data
    corruption and force a re-auth that the operator would never understand.
    """
    if not DIGIKEY_TOKEN_CACHE.exists():
        return None
    try:
        return json.loads(DIGIKEY_TOKEN_CACHE.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Token cache at {DIGIKEY_TOKEN_CACHE} is not valid JSON ({e}). "
            f"Delete the file and re-bootstrap via DIGIKEY_REFRESH_TOKEN_SEED."
        ) from e


def _write_token_cache(state: dict) -> None:
    """Atomically persist tokens to the cache file with 0600 perms.

    os.open with mode=0o600 makes the kernel apply 0600 at creation, closing the
    umask race that write_text + chmod has. O_TRUNC silently overwrites any stale
    .tmp from a crashed prior writer — same single-process invariant as the
    atomic os.replace below.

    Failure is non-fatal — DigiKey deployments without a writable cache path
    still work for the life of the process; they just can't survive a restart.
    """
    global _TOKEN_CACHE_WRITE_OK
    try:
        DIGIKEY_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DIGIKEY_TOKEN_CACHE.with_suffix(DIGIKEY_TOKEN_CACHE.suffix + ".tmp")
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, indent=2))
        os.replace(tmp, DIGIKEY_TOKEN_CACHE)
        _TOKEN_CACHE_WRITE_OK = True
    except OSError as e:
        if _TOKEN_CACHE_WRITE_OK:
            # Log once per failure-streak so a read-only deployment doesn't spam logs.
            logger.warning(
                "Could not persist token cache to %s (%s). Tokens will live in memory "
                "only; the next process restart will need a fresh DIGIKEY_REFRESH_TOKEN_SEED.",
                DIGIKEY_TOKEN_CACHE, e,
            )
            _TOKEN_CACHE_WRITE_OK = False


def _refresh_user_access_token(refresh_token: str) -> dict:
    """Exchange a refresh_token for a new access_token. Returns the full token response.

    DigiKey rotates the refresh_token on every call — the response dict's
    'refresh_token' field is a NEW value and the input becomes invalid immediately.
    Callers must persist the new value or the next refresh will fail with invalid_grant.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("CLIENT_ID and CLIENT_SECRET must be set to refresh MyLists tokens.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    resp = requests.post(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_REQUEST_TIMEOUT_SECS,
    )
    if resp.status_code != 200:
        # Surface DigiKey's error verbatim — 'invalid_grant' means the refresh token
        # was rotated out from under us (server restart with stale seed) or the user
        # revoked access. Either way the operator needs to re-bootstrap.
        raise RuntimeError(
            f"DigiKey token refresh failed ({resp.status_code}): {resp.text}. "
            f"Run `digikey-mcp-auth login` locally and update DIGIKEY_REFRESH_TOKEN_SEED."
        )
    return resp.json()


def _get_user_access_token() -> str:
    """Return a valid user-scoped access token, refreshing if needed.

    Thread-safe: holds a lock around the refresh so concurrent tool calls don't issue
    multiple refreshes (only one would succeed — DigiKey invalidates the prior refresh
    token on use, so a second concurrent refresh would 400).
    """
    with _USER_TOKEN_LOCK:
        # Load from cache on first use, falling back to the seed env var.
        if _USER_TOKEN_STATE["refresh_token"] is None:
            cached = _read_token_cache()
            if cached and cached.get("refresh_token"):
                _USER_TOKEN_STATE.update(cached)
                logger.info("Loaded MyLists tokens from %s", DIGIKEY_TOKEN_CACHE)
            elif DIGIKEY_REFRESH_TOKEN_SEED:
                _USER_TOKEN_STATE["refresh_token"] = DIGIKEY_REFRESH_TOKEN_SEED
                logger.info("Bootstrapping MyLists token cache from DIGIKEY_REFRESH_TOKEN_SEED.")
            else:
                raise RuntimeError(
                    "MyLists tools require user-context auth, but no refresh token is "
                    f"cached at {DIGIKEY_TOKEN_CACHE} and DIGIKEY_REFRESH_TOKEN_SEED is "
                    f"not set. Run `digikey-mcp-auth login` locally, then set "
                    f"DIGIKEY_REFRESH_TOKEN_SEED to the value it prints (or write "
                    f"directly to the cache path)."
                )

        now = int(time.time())
        if _USER_TOKEN_STATE["access_token"] and now < _USER_TOKEN_STATE["expires_at"] - _TOKEN_REFRESH_LEEWAY_SECS:
            return _USER_TOKEN_STATE["access_token"]

        tokens = _refresh_user_access_token(_USER_TOKEN_STATE["refresh_token"])
        _USER_TOKEN_STATE["access_token"] = tokens["access_token"]
        # DigiKey rotates the refresh_token; falling back to the old one would brick
        # the cache on the next refresh.
        _USER_TOKEN_STATE["refresh_token"] = tokens.get("refresh_token", _USER_TOKEN_STATE["refresh_token"])
        _USER_TOKEN_STATE["expires_at"] = now + int(tokens.get("expires_in", 1799))
        _write_token_cache(dict(_USER_TOKEN_STATE))
        return _USER_TOKEN_STATE["access_token"]


def _get_user_headers() -> dict:
    """Headers for user-context (MyLists) requests."""
    headers = {
        "Authorization": f"Bearer {_get_user_access_token()}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "Content-Type": "application/json",
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
    }
    if DIGIKEY_ACCOUNT_ID:
        headers["X-DIGIKEY-Account-Id"] = DIGIKEY_ACCOUNT_ID
    return headers


def _make_user_request(method: str, url: str, data=None, params: dict = None) -> dict | list | None:
    """Make a MyLists API request with the user-scoped token. Mirrors _make_request
    but takes its headers from _get_user_headers and supports non-JSON empty responses
    (DELETE returns 204).

    Tests intercept this in conftest.py the same way they intercept _make_request.
    """
    headers = _get_user_headers()
    if params:
        # urlencode handles reserved chars properly (spaces, &, =, …); the prior
        # f"{k}={v}" form silently produced malformed URLs for any value with
        # special characters. doseq=False is fine — none of our params are lists.
        encoded = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        if encoded:
            url = f"{url}?{encoded}"

    logger.info(f"Making {method} {url} (user-context)")
    if data is not None:
        logger.debug(f"Request body: {json.dumps(data, indent=2)}")

    method_upper = method.upper()
    if method_upper == "GET":
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECS)
    elif method_upper == "DELETE":
        resp = requests.delete(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECS)
    elif method_upper == "POST":
        resp = requests.post(url, headers=headers, json=data, timeout=_REQUEST_TIMEOUT_SECS)
    elif method_upper == "PUT":
        resp = requests.put(url, headers=headers, json=data, timeout=_REQUEST_TIMEOUT_SECS)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    logger.info(f"Response status: {resp.status_code}")
    if not resp.ok:
        logger.error(f"MyLists API error: {resp.status_code} - {resp.text}")
        resp.raise_for_status()

    # 204 No Content or other empty bodies — DELETE in particular.
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


_CATEGORY_NAME_CACHE: dict = {}


def _get_category_name(category_id) -> str:
    """Resolve a DigiKey category id to its leaf-category name (e.g. 58 → 'Aluminum Electrolytic Capacitors').

    Cached per-process. Used to seed `Keywords` for category-scoped searches: the DigiKey v4
    API requires a non-trivial Keywords value, and using the category's own name as the keyword
    is what makes CategoryFilter actually return the full category set with rich facets.
    """
    key = str(category_id)
    if key in _CATEGORY_NAME_CACHE:
        return _CATEGORY_NAME_CACHE[key]

    raw = _make_request("GET", f"{API_BASE}/products/v4/search/categories/{key}", _get_headers())

    def _walk(node):
        if str(node.get("CategoryId")) == key:
            return node.get("Name")
        for c in node.get("Children") or []:
            r = _walk(c)
            if r:
                return r
        return None

    name = _walk(raw.get("Category") or {})
    if not name:
        raise ValueError(f"Category id {category_id!r} not found in DigiKey category tree.")
    _CATEGORY_NAME_CACHE[key] = name
    return name


def _build_parameter_filter_request(category_id: str, parameter_filters: dict) -> dict:
    """Build a ParameterFilterRequest body from {parameter_id: [value_id, ...]}."""
    if not category_id:
        raise ValueError("category_id is required when parameter_filters is set")

    parameter_filter_list = []
    for param_id, value_ids in parameter_filters.items():
        if isinstance(value_ids, (str, int)):
            value_ids = [value_ids]
        parameter_filter_list.append({
            "ParameterId": int(param_id),
            "FilterValues": [{"Id": str(v)} for v in value_ids],
        })

    return {
        "CategoryFilter": {"Id": str(category_id)},
        "ParameterFilters": parameter_filter_list,
    }


def _do_keyword_search(keywords: str, limit: int = 5, manufacturer_id: str = None, category_id: str = None, search_options: str = None, sort_field: str = None, sort_order: str = "Ascending", parameter_filters: dict = None, use_category_as_keyword: bool = False):
    """Internal: POST /search/keyword with optional parametric filtering. Not exposed as a tool.

    DigiKey rejects empty Keywords with a 400, and treats "*" as a literal — neither gives a
    full category browse. Set use_category_as_keyword=True (find_components does this) to seed
    Keywords with the category's own name, which is the only known way to get a complete
    category-scoped result set with rich parametric facets.

    Filter fields are nested under FilterOptionsRequest per the v4 spec; putting them at the
    top level causes the API to silently ignore them.
    """
    url = f"{API_BASE}/products/v4/search/keyword"
    headers = _get_headers()

    if not keywords and use_category_as_keyword and category_id:
        keywords = _get_category_name(category_id)

    body = {
        "Keywords": keywords or "*",
        "Limit": limit,
    }

    filter_options = {}
    if category_id:
        filter_options["CategoryFilter"] = [{"Id": str(category_id)}]
    if manufacturer_id:
        filter_options["ManufacturerFilter"] = [{"Id": str(manufacturer_id)}]
    if search_options:
        filter_options["SearchOptions"] = [s.strip() for s in search_options.split(",") if s.strip()]
    if parameter_filters:
        filter_options["ParameterFilterRequest"] = _build_parameter_filter_request(category_id, parameter_filters)

    if filter_options:
        body["FilterOptionsRequest"] = filter_options

    if sort_field:
        body["SortOptions"] = {
            "Field": sort_field,
            "SortOrder": sort_order,
        }

    return _make_request("POST", url, headers, body)


@mcp.tool()
def keyword_search(keywords: str, limit: int = 5, manufacturer_id: str = None, category_id: str = None, search_options: str = None, sort_field: str = None, sort_order: str = "Ascending"):
    """Free-text search of DigiKey products by keyword or part number.

    For attribute-based queries (capacitance, diameter, etc.), use find_components instead.

    Args:
        keywords: Search terms or part numbers
        limit: Maximum number of results (default: 5)
        manufacturer_id: Filter by specific manufacturer ID
        category_id: Filter by specific category ID
        search_options: Comma-delimited values from the v4 SearchOptions enum. Valid values:
            ChipOutpost, Has3DModel, HasCadModel, HasDatasheet, HasProductPhoto, InStock,
            NewProduct, NonRohsCompliant, NormallyStocking, RohsCompliant. Note case
            (RohsCompliant, not RoHSCompliant) — DigiKey ignores unknown values silently.
        sort_field: Field to sort by. Options: None, Packaging, ProductStatus, DigiKeyProductNumber, ManufacturerProductNumber, Manufacturer, MinimumQuantity, QuantityAvailable, Price, Supplier, PriceManufacturerStandardPackage
        sort_order: Sort direction - Ascending or Descending (default: Ascending)
    """
    # Empty Keywords would silently fall through to the "*" placeholder downstream — which
    # DigiKey treats as a literal-character match (yielding a tiny, near-meaningless result
    # set), not as "give me everything". Surface the problem instead.
    if not keywords or not keywords.strip():
        raise ValueError(
            "keywords must be a non-empty string. "
            "For attribute-based search use find_components; for a category browse use "
            "find_components(category_id, ...) with no keyword."
        )
    return _do_keyword_search(
        keywords=keywords,
        limit=limit,
        manufacturer_id=manufacturer_id,
        category_id=category_id,
        search_options=search_options,
        sort_field=sort_field,
        sort_order=sort_order,
    )


def _get_parametric_filters(category_id: str, keywords: str = "", limit: int = 1):
    """Internal: fetch parametric filter options for a category. Not exposed as a tool.

    Uses the category's own name as Keywords when no override is supplied — this is what
    makes DigiKey return the full facet histogram for the category. Passing 'Keywords="*"'
    or an empty string yields a sparse 1-3-value histogram because DigiKey treats them as
    literal keyword matches, not wildcards.
    """
    url = f"{API_BASE}/products/v4/search/keyword"
    headers = _get_headers()
    body = {
        "Keywords": keywords or _get_category_name(category_id),
        "Limit": limit,
        "FilterOptionsRequest": {
            "CategoryFilter": [{"Id": str(category_id)}],
        },
    }
    raw = _make_request("POST", url, headers, body)

    parametric = (raw.get("FilterOptions") or {}).get("ParametricFilters") or []
    return [
        {
            "ParameterId": p.get("ParameterId"),
            "ParameterName": p.get("ParameterName"),
            "ParameterType": p.get("ParameterType"),
            "FilterValues": [
                {
                    "ValueId": v.get("ValueId"),
                    "ValueName": v.get("ValueName"),
                    "ProductCount": v.get("ProductCount"),
                    "RangeFilterType": v.get("RangeFilterType"),
                }
                for v in (p.get("FilterValues") or [])
            ],
        }
        for p in parametric
    ]


@mcp.tool()
def get_parametric_filters(
    category_id: str,
    parameter_name: str = None,
    max_values: int = 100,
    keywords: str = "",
):
    """Discover the parametric filters available for a category.

    By default returns a SUMMARY — one entry per parameter with its name, type, total value
    count, and three sample values. To get the actual ValueIds/ValueNames for one parameter,
    call again with `parameter_name="Capacitance"` (fuzzy-matched).

    Args:
        category_id: The DigiKey category to inspect.
        parameter_name: If set, return FilterValues for this parameter only.
        max_values: Max FilterValues to return (top by ProductCount). Default 100.
            Set to 0 for unlimited.
        keywords: Optional override for the keyword that scopes the facet histogram. When
            empty (default) the category's own name is used.

    Returns:
        Summary form (default):
            [{ParameterName, ParameterType, TotalCount, SampleValues: [...]}]
        Per-parameter form (when parameter_name is set):
            {ParameterId, ParameterName, ParameterType, TotalCount, Truncated, FilterValues: [...]}
    """
    filters = _get_parametric_filters(category_id=category_id, keywords=keywords)

    if parameter_name:
        param = _match_parameter(parameter_name, filters)
        all_values = param.get("FilterValues") or []
        ranked = sorted(all_values, key=lambda v: -(v.get("ProductCount") or 0))
        truncated = max_values > 0 and len(ranked) > max_values
        values = ranked[:max_values] if max_values > 0 else ranked
        return {
            "ParameterId": param.get("ParameterId"),
            "ParameterName": param.get("ParameterName"),
            "ParameterType": param.get("ParameterType"),
            "TotalCount": len(all_values),
            "Truncated": truncated,
            "FilterValues": values,
        }

    return [
        {
            "ParameterName": p.get("ParameterName"),
            "ParameterType": p.get("ParameterType"),
            "TotalCount": len(p.get("FilterValues") or []),
            "SampleValues": [
                v.get("ValueName")
                for v in sorted(
                    p.get("FilterValues") or [],
                    key=lambda v: -(v.get("ProductCount") or 0),
                )[:3]
            ],
        }
        for p in filters
    ]


def _normalize_text(s) -> str:
    """Normalize for fuzzy matching: lowercase, collapse whitespace, fold micro signs."""
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = s.replace("µ", "u").replace("μ", "u")
    s = re.sub(r"\s+", " ", s)
    return s


def _suggest_close_names(target: str, names: list, n: int = 5) -> list:
    """Top-n names by edit-distance similarity to target. Falls back to the first n
    sorted names when nothing scores above the cutoff."""
    matches = difflib.get_close_matches(str(target), [n for n in names if n], n=n, cutoff=0.5)
    return matches or sorted(n for n in names if n)[:n]


def _suggest_close_values(target, available_values: list, n: int = 5) -> list:
    """Top-n FilterValue names closest to target.

    If target parses as a pint Quantity, prefers magnitude proximity (the natural
    notion of "close" for unit-bearing values). Otherwise falls back to edit-distance
    string similarity. Catches the typo-470-as-473 case the old "20 smallest sorted
    ascending" behavior failed to help with.
    """
    target_qty = _to_quantity(target)
    if target_qty is not None:
        try:
            target_mag = target_qty.to_base_units().magnitude
        except Exception:
            target_mag = None
        if target_mag is not None:
            scored = []
            for fv in available_values:
                fv_qty = _to_quantity(fv.get("ValueName"))
                if fv_qty is None:
                    continue
                try:
                    mag = fv_qty.to_base_units().magnitude
                    scored.append((abs(mag - target_mag), fv.get("ValueName")))
                except Exception:
                    continue
            if scored:
                scored.sort()
                return [name for _, name in scored[:n]]
    names = [fv.get("ValueName") for fv in available_values if fv.get("ValueName")]
    matches = difflib.get_close_matches(str(target), names, n=n, cutoff=0.3)
    return matches or names[:n]


def _match_parameter(name: str, available: list) -> dict:
    """Find a parameter in the available list by name. Raises ValueError with candidates if ambiguous/missing."""
    target = _normalize_text(name)
    exact = [p for p in available if _normalize_text(p.get("ParameterName")) == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        names = [p.get("ParameterName") for p in exact]
        raise ValueError(f"Attribute name '{name}' matched multiple parameters: {names}")

    contains = [p for p in available if target and target in _normalize_text(p.get("ParameterName"))]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        names = [p.get("ParameterName") for p in contains]
        raise ValueError(f"Attribute name '{name}' is ambiguous; candidates: {names}")

    all_names = sorted({p.get("ParameterName") for p in available if p.get("ParameterName")})
    close = _suggest_close_names(name, all_names)
    raise ValueError(
        f"Attribute name {name!r} not found in category. "
        f"Did you mean: {close}? ({len(all_names)} attributes total)"
    )


_UREG = pint.UnitRegistry()
# DigiKey writes spelled-out unit names with capitalization and plurals that pint's
# default registry rejects ('Ohms', 'kOhms', 'Henries', 'Volts', etc.). Register them
# as aliases — pint then handles SI prefixes on top automatically, so '4.7 kOhms' and
# '100 µOhms' parse correctly without any further work.
for _alias_def in (
    "@alias ohm = Ohm = Ohms = ohms",
    "@alias hertz = Hertz",
    "@alias henry = Henries = henries = Henry",
    "@alias farad = Farad = Farads = farads",
    "@alias volt = Volt = Volts = volts",
    "@alias ampere = Amp = Amps = amps",
    "@alias watt = Watt = Watts = watts",
):
    _UREG.define(_alias_def)
# Operators/separators that signal a compound expression DigiKey uses for non-quantity
# value formatting. We reject these before pint evaluates them as math (see _to_quantity).
#   '@'  — DigiKey's coupled-value separator (Ripple Current @ 100 kHz). Pint reads as
#          matrix multiplication, falls back to scalar product. Silent garbage.
#   '/'  followed by digits — slash-separated alternatives like '100/120/200V'. Pint
#          evaluates as division.
#   ','  — comma-separated values; pint behavior is parser-version-dependent.
_COMPOUND_OPERATOR_RE = re.compile(r"@|/\s*\d|,")


def _to_quantity(value):
    """Parse a DigiKey filter value (e.g. '470 µF', '12.5 mm', '1 kΩ') to a pint Quantity.

    Returns None when the value isn't a clean unit string — e.g. '8000 Hrs @ 105°C'
    (undefined units), an empty/whitespace string, or a malformed input. Callers should
    treat None as "not comparable" and fall back to exact-string matching.

    The except clause is narrowed to the exception types pint actually raises for invalid
    inputs (observed across DigiKey's filter-value corpus). Genuine bugs — typos against
    the pint API, ImportError, etc. — propagate so they surface during development rather
    than being silently swallowed as "this value can't be parsed."
    """
    if value is None:
        return None
    s = str(value).strip().lstrip("±")
    # Pre-filter compound expressions before pint sees them. Pint's parser reuses Python's
    # AST and silently evaluates operators — '500 mA @ 100 kHz' becomes a scalar product
    # (50000 mA·kHz), '100/120/200V' becomes a fraction, etc. The resulting Quantity has
    # the wrong dimensionality for our use case but compares "successfully" against other
    # similarly-mangled values, producing silent-garbage filter results. None here forces
    # callers (e.g. range matching) to either fall back to discrete matching or error out
    # cleanly via their None-check.
    if _COMPOUND_OPERATOR_RE.search(s):
        return None
    try:
        return _UREG.Quantity(s)
    except (pint.PintError, ValueError, TypeError, AssertionError):
        return None


def _match_values(param: dict, values) -> list:
    """Resolve user-supplied value strings to ValueIds for a given parameter.

    `values` can be:
      - a single string/number/int (e.g. '470 µF')
      - a list of the above
      - a dict {'min': X, 'max': Y} for numeric ranges (either bound optional). When supplied,
        matches all FilterValues whose parsed numeric magnitude is within [min, max].

    Raises ValueError with close candidates on a miss.
    """
    available_values = param.get("FilterValues") or []

    # Range form: {"min": ..., "max": ...}
    if isinstance(values, dict) and ("min" in values or "max" in values):
        # Coupled-unit parameters (e.g. 'Ripple Current @ Low Frequency', valued like
        # '500 mA @ 100 kHz') describe two axes — a current and a frequency. There's no
        # single scalar magnitude to compare against, so a range query is semantically
        # ill-defined: pint would silently compute (current × frequency) and produce
        # plausible-looking but wrong results. Refuse it explicitly.
        if param.get("ParameterType") == "CoupledUnitOfMeasure":
            raise ValueError(
                f"Range matching is not supported on '{param.get('ParameterName')}' — it's a "
                f"coupled-unit parameter (two axes per value, e.g. current @ frequency) and the "
                f"two axes can't be reduced to a single magnitude. Use a discrete value or a "
                f"list of values from the histogram instead."
            )
        lo = _to_quantity(values.get("min"))
        hi = _to_quantity(values.get("max"))
        sample = [fv.get("ValueName") for fv in available_values[:10]]
        if values.get("min") is not None and lo is None:
            raise ValueError(
                f"Could not parse range minimum {values['min']!r} for '{param.get('ParameterName')}' "
                f"as a unit-bearing quantity. Use a discrete value from the histogram instead. "
                f"Sample values: {sample}"
            )
        if values.get("max") is not None and hi is None:
            raise ValueError(
                f"Could not parse range maximum {values['max']!r} for '{param.get('ParameterName')}' "
                f"as a unit-bearing quantity. Use a discrete value from the histogram instead. "
                f"Sample values: {sample}"
            )
        resolved = []
        for fv in available_values:
            q = _to_quantity(fv.get("ValueName"))
            if q is None:
                continue
            try:
                if lo is not None and q < lo:
                    continue
                if hi is not None and q > hi:
                    continue
            except (pint.DimensionalityError, pint.OffsetUnitCalculusError, TypeError):
                continue
            resolved.append(fv.get("ValueId"))
        if not resolved:
            raise ValueError(
                f"No values for '{param.get('ParameterName')}' match range {values}. "
                f"Sample values: {sample}"
            )
        return resolved

    if isinstance(values, (str, int, float)):
        values = [values]
    resolved = []
    for v in values:
        target = _normalize_text(v)
        exact = [fv for fv in available_values if _normalize_text(fv.get("ValueName")) == target]
        primary = exact[0] if len(exact) == 1 else None
        if not primary:
            contains = [fv for fv in available_values if target and target in _normalize_text(fv.get("ValueName"))]
            if len(contains) == 1:
                primary = contains[0]
            elif len(contains) > 1:
                names = [fv.get("ValueName") for fv in contains]
                raise ValueError(f"Value '{v}' for '{param.get('ParameterName')}' is ambiguous: {names}")

        # Expand to magnitude aliases. DigiKey enumerates the same physical value under
        # multiple unit strings as distinct buckets ('1 mF' and '1000 µF'). Empirically
        # both return the same product set (test report) but we don't know that's
        # universal — a product tagged only under one alias would be silently dropped if
        # we passed just the literal match. Always send DigiKey every magnitude-equivalent
        # ValueId so the result is a union; display-side dedup happens separately.
        if primary:
            matched_fvs = _expand_to_magnitude_aliases(primary, available_values)
        else:
            # No string-based hit — try a quantity-magnitude match (cross-prefix input
            # like '0.47 mF' when the histogram only enumerates '470 µF', or '100 nF'
            # vs '0.1 µF'). Use _magnitude_key rather than pint's `==` because the
            # latter can return False for physically-equal cross-prefix quantities due
            # to float-rounding artefacts (1e-07 vs 1.0000000000000001e-07).
            user_qty = _to_quantity(v)
            matched_fvs = []
            if user_qty is not None:
                try:
                    user_key = _magnitude_key(user_qty)
                except Exception:
                    user_key = None
                for fv in available_values:
                    fv_qty = _to_quantity(fv.get("ValueName"))
                    if fv_qty is None:
                        continue
                    try:
                        if (
                            user_key is not None
                            and user_qty.dimensionality == fv_qty.dimensionality
                            and _magnitude_key(fv_qty) == user_key
                        ):
                            matched_fvs.append(fv)
                    except (pint.DimensionalityError, pint.OffsetUnitCalculusError, TypeError):
                        continue

        if not matched_fvs:
            close = _suggest_close_values(v, available_values)
            raise ValueError(
                f"Value {v!r} not found for '{param.get('ParameterName')}'. "
                f"Did you mean: {close}? ({len(available_values)} values total)"
            )
        for fv in matched_fvs:
            resolved.append(fv.get("ValueId"))
    return resolved


def _expand_to_magnitude_aliases(primary: dict, available: list) -> list:
    """Return primary plus every FilterValue with the same base-unit magnitude.

    Defensive against DigiKey tagging the same physical value under multiple unit
    strings as separate buckets ('1 mF' / '1000 µF'). We don't know if DigiKey
    always aliases internally — passing the union of ValueIds guarantees we don't
    drop products that happen to be tagged under a different alias. Primary always
    comes first so display order is stable.
    """
    qty = _to_quantity(primary.get("ValueName"))
    if qty is None:
        return [primary]
    try:
        target_key = _magnitude_key(qty)
    except Exception:
        return [primary]
    aliases = [primary]
    for fv in available:
        if fv is primary:
            continue
        other = _to_quantity(fv.get("ValueName"))
        if other is None:
            continue
        try:
            if _magnitude_key(other) == target_key:
                aliases.append(fv)
        except Exception:
            continue
    return aliases


def _magnitude_key(q):
    """Stable comparison/hash key for a pint Quantity's base-unit magnitude.

    Pint's prefix conversion can introduce tiny float-rounding artefacts that make
    physically-equal values compare unequal (`0.1 µF`.magnitude == 1e-07 but
    `100 nF`.magnitude == 1.0000000000000001e-07). Rounding to 9 significant figures
    is well below DigiKey's catalog precision (typically 3–4 sig figs) while wider
    than every float-rounding artefact pint produces.
    """
    return float(f"{q.to_base_units().magnitude:.9e}")


def _sort_by_magnitude(names: list) -> list:
    """Return names sorted ascending by base-unit magnitude. Names that don't parse via
    pint are dropped (used only to compute From/To bounds, where unparseable values
    can't be meaningfully ordered against each other anyway)."""
    pairs = []
    for n in names:
        q = _to_quantity(n)
        if q is None:
            continue
        try:
            pairs.append((q.to_base_units().magnitude, n))
        except Exception:
            continue
    pairs.sort()
    return [n for _, n in pairs]


def _dedupe_by_magnitude(names: list) -> list:
    """Collapse multiple unit-string representations of the same physical value into one.

    DigiKey enumerates the same magnitude under multiple unit strings as separate buckets
    ('1 mF' and '1000 µF', '0.1 µF' and '100 nF', etc.). For a range query they all match
    the same product set, so reporting them as distinct matches inflates the count and
    bloats the Sample. Keep the first occurrence per base-unit magnitude (DigiKey orders
    histograms by ProductCount desc, so this is the most popular alias). Values that don't
    parse via pint pass through unchanged — we can't normalize what we can't measure.
    """
    seen = set()
    out = []
    for name in names:
        q = _to_quantity(name)
        if q is None:
            out.append(name)
            continue
        try:
            key = _magnitude_key(q)
        except Exception:
            out.append(name)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _slim_product(product: dict) -> dict:
    """Trim a DigiKey Product to the fields most useful in an LLM context.

    Fields straight-from-DigiKey keep their original PascalCase name. Fields where we change
    the *shape* (object → string, list-of-objects → flat dict, etc.) get a distinct name so
    callers reading DigiKey's docs don't get misled:
      - ManufacturerName: the .Name extracted from DigiKey's Manufacturer object
      - ProductDescription: the short description string from DigiKey's Description object
      - ParameterMap: name→value flat dict reduced from DigiKey's Parameters list-of-objects
      - DigiKeyProductNumber: the primary variation's DK PN. DigiKey itself only exposes this
        nested in ProductVariations[], so reusing the name at top level doesn't shadow anything.
    """
    variations = product.get("ProductVariations") or []
    manufacturer = product.get("Manufacturer") or {}
    description = product.get("Description") or {}
    return {
        "ManufacturerProductNumber": product.get("ManufacturerProductNumber"),
        "DigiKeyProductNumber": variations[0].get("DigiKeyProductNumber") if variations else None,
        "ManufacturerName": manufacturer.get("Name") or manufacturer.get("Value"),
        "ProductDescription": description.get("ProductDescription"),
        "UnitPrice": product.get("UnitPrice"),
        "QuantityAvailable": product.get("QuantityAvailable"),
        "DatasheetUrl": product.get("DatasheetUrl"),
        "ProductUrl": product.get("ProductUrl"),
        "ParameterMap": {
            pv.get("ParameterText"): pv.get("ValueText")
            for pv in (product.get("Parameters") or [])
            if pv.get("ParameterText")
        },
    }


@mcp.tool()
def find_components(
    category_id: str,
    attributes: dict = None,
    keywords: str = "",
    limit: int = 25,
    in_stock_only: bool = False,
):
    """High-level parametric component search. Resolves human-readable attribute names/values to DigiKey ids and returns slim results.

    Examples:
        # Discrete value match
        find_components(
            category_id="58",
            attributes={"Capacitance": "470 µF"},
            in_stock_only=True,
        )

        # Min/max range — finds all values whose magnitude falls in the window
        find_components(
            category_id="58",
            attributes={
                "Capacitance": {"min": "100 µF", "max": "1000 µF"},
                "Diameter - Seated (Max)": {"max": "10mm"},
            },
        )

    Args:
        category_id: Numeric DigiKey category id (use search_categories to find it).
        attributes: Mapping of attribute name -> value spec. Each value spec may be:
            * a single value string ("470 µF")
            * a list of values (["10mm", "12.5mm"]) — matches any
            * a range dict ({"min": "100 µF", "max": "1000 µF"}) — either bound optional
            Names and discrete values are matched case-insensitively with micro-sign folding.
            Errors list close candidates so the model can retry.
        keywords: Optional free-text keywords. When omitted, the category's own name is used
            as Keywords — this is what makes DigiKey return the full category set.
        limit: Max results to return (default 25, DigiKey caps at 50).
        in_stock_only: If True, restrict to in-stock products.

    Returns:
        {"ProductsCount": int, "AppliedFilters": {...}, "Products": [slim_product, ...]}

    Note on sorting: DigiKey can't sort by parametric attributes server-side, and sorting
    the returned page client-side would be misleading (it would sort N results from a much
    larger matching set, not the global top-N). To get the actual top-K by some attribute,
    narrow the query with parametric filters until the result set fits in one page, then
    sort the returned products[] in your own code.
    """
    # Normalize whitespace so '   ' doesn't slip through to DigiKey as a literal-character
    # keyword match (the same issue keyword_search rejects). Empty after strip → use the
    # category-name fallback downstream.
    keywords = (keywords or "").strip()

    # Only fetch the parametric histogram when the caller actually needs to resolve
    # attribute names — keyword-only calls (no attributes) don't, and the discovery
    # request is a wasted POST otherwise.
    filters_meta = []
    if attributes:
        filters_meta = _get_parametric_filters(category_id=category_id, keywords="", limit=1)
        # Broad parent categories (e.g. cat 20 Connectors, cat 32 Integrated Circuits) return
        # no parametric filters — DigiKey only computes facets at the leaf level. The error
        # from _match_parameter ("Available: []") would point at the attribute name when the
        # real problem is the category.
        if not filters_meta:
            cat_name = _get_category_name(category_id)
            raise ValueError(
                f"Category {category_id} ({cat_name!r}) has no parametric attributes — DigiKey "
                f"computes facets only at the leaf level, and this appears to be a parent category. "
                f"Use search_categories or get_category_by_id to find a leaf subcategory."
            )

    parameter_filters = {}
    applied = {}
    for name, values in (attributes or {}).items():
        is_range = isinstance(values, dict) and ("min" in values or "max" in values)
        param = _match_parameter(name, filters_meta)
        value_ids = _match_values(param, values)
        parameter_filters[param["ParameterId"]] = value_ids
        matched_names = [
            fv.get("ValueName")
            for fv in (param.get("FilterValues") or [])
            if fv.get("ValueId") in value_ids
        ]
        # The shape of `applied` signals the kind of query. A list says "the user asked for
        # these specific values" (and the caller probably wants to see them all). A summary
        # dict says "the user asked for a range" — the literal list isn't what they specified,
        # and dumping hundreds of bucket names back at them isn't informative.
        if is_range:
            distinct_names = _dedupe_by_magnitude(matched_names)
            # From/To must reflect the actual numeric bounds of the matched set, not
            # DigiKey's response order (which is by ProductCount desc — so the old code
            # was labelling "the most popular alias" as From and "the least popular" as
            # To, which is wrong). Sample stays in popularity order — that's what makes
            # it informative.
            by_magnitude = _sort_by_magnitude(distinct_names)
            applied[param.get("ParameterName")] = {
                "MatchedCount": len(distinct_names),
                "From": by_magnitude[0] if by_magnitude else (distinct_names[0] if distinct_names else None),
                "To": by_magnitude[-1] if by_magnitude else (distinct_names[-1] if distinct_names else None),
                "Sample": distinct_names[:5],
            }
        else:
            applied[param.get("ParameterName")] = matched_names

    raw = _do_keyword_search(
        keywords=keywords,
        limit=limit,
        category_id=str(category_id),
        search_options="InStock" if in_stock_only else None,
        parameter_filters=parameter_filters or None,
        use_category_as_keyword=True,
    )

    products = [_slim_product(p) for p in (raw.get("Products") or [])]

    return {
        "ProductsCount": raw.get("ProductsCount"),
        "AppliedFilters": applied,
        "Products": products,
    }


@mcp.tool()
def product_details(product_number: str, manufacturer_id: str = None, customer_id: str = "0"):
    """Get detailed information for a specific product.
    
    Args:
        product_number: DigiKey or manufacturer part number
        manufacturer_id: Optional manufacturer ID for disambiguation
        customer_id: Customer ID for pricing (default: "0")
    """
    url = f"{API_BASE}/products/v4/search/{product_number}/productdetails"
    headers = _get_headers(customer_id)
    
    params = {}
    if manufacturer_id:
        params["manufacturerId"] = manufacturer_id
    
    if params:
        url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
    
    return _make_request("GET", url, headers)

@mcp.tool()
def search_manufacturers():
    """Search and retrieve all product manufacturers."""
    url = f"{API_BASE}/products/v4/search/manufacturers"
    headers = _get_headers()
    return _make_request("GET", url, headers)

@mcp.tool()
def search_categories():
    """Search and retrieve all product categories."""
    url = f"{API_BASE}/products/v4/search/categories"
    headers = _get_headers()
    return _make_request("GET", url, headers)

@mcp.tool()
def get_category_by_id(category_id: int):
    """Get specific category details by ID.
    
    Args:
        category_id: The category ID to retrieve
    """
    url = f"{API_BASE}/products/v4/search/categories/{category_id}"
    headers = _get_headers()
    return _make_request("GET", url, headers)

@mcp.tool()
def search_product_substitutions(product_number: str, limit: int = 10, search_options: str = None, exclude_marketplace: bool = False):
    """Search for product substitutions for a given product.
    
    Args:
        product_number: The product to get substitutions for
        limit: Number of substitutions (default: 10)
        search_options: Filters like LeadFree,RoHSCompliant,InStock
        exclude_marketplace: Exclude marketplace products (default: False)
    """
    url = f"{API_BASE}/products/v4/search/{product_number}/substitutions"
    headers = _get_headers()
    
    params = {"limit": limit, "excludeMarketPlaceProducts": exclude_marketplace}
    if search_options:
        params["searchOptionList"] = search_options
    
    url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
    return _make_request("GET", url, headers)

@mcp.tool()
def get_product_media(product_number: str):
    """Get media (images, documents, videos) for a product.
    
    Args:
        product_number: The product to get media for
    """
    url = f"{API_BASE}/products/v4/search/{product_number}/media"
    headers = _get_headers()
    return _make_request("GET", url, headers)

@mcp.tool()
def get_product_pricing(product_number: str, customer_id: str = "0", requested_quantity: int = 1):
    """Get detailed pricing information for a product.
    
    Args:
        product_number: The product to get pricing for
        customer_id: Customer ID for pricing (default: "0")
        requested_quantity: Quantity for pricing calculation (default: 1)
    """
    url = f"{API_BASE}/products/v4/search/{product_number}/productpricing"
    headers = _get_headers(customer_id)
    
    params = {"requestedQuantity": requested_quantity}
    url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
    
    return _make_request("GET", url, headers)

@mcp.tool()
def get_digi_reel_pricing(product_number: str, requested_quantity: int, customer_id: str = "0"):
    """Get DigiReel pricing for a product.
    
    Args:
        product_number: DigiKey product number (must be DigiReel compatible)
        requested_quantity: Quantity for DigiReel pricing
        customer_id: Customer ID for pricing (default: "0")
    """
    url = f"{API_BASE}/products/v4/search/{product_number}/digireelpricing"
    headers = _get_headers(customer_id)
    
    params = {"requestedQuantity": requested_quantity}
    url += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
    
    return _make_request("GET", url, headers)


# =============================================================================
# User-scoped tools — DigiKey APIs that require 3-legged OAuth (per-customer
# refresh token), as opposed to the client_credentials grant the Product Search
# tools use. MyLists v1 is the first such surface; Orders / Cart / etc. would
# share the same gate when added.
#
# Registration is CONDITIONAL on user-context auth being plausibly configured
# (DIGIKEY_REFRESH_TOKEN_SEED env var present OR cache file exists at module
# load). On a deployment without user-auth set up, an agent sees the Product
# Search tools only — user-scoped tools don't appear in tools/list at all,
# rather than appearing and exploding at call time. This matches the pattern in
# atlassian-mcp-server / mcp-atlassian (servers with mixed auth contexts that
# can operate degraded); google-drive-mcp / linear-mcp (single-context servers)
# take the opposite approach. Our split between always-on client_credentials
# and optional refresh_token maps to the conditional side.
#
# The check happens at module import, so adding user-auth post-startup requires
# a restart — fine for our deployment shapes (mcpjungle / sidecar config
# changes already imply restart).
# =============================================================================


def _user_auth_available() -> bool:
    """True when user-scoped tools should be exposed.

    A seed env var or an existing cache file is enough — we don't try to refresh
    here because (1) it's slow at import time, (2) it would couple module load
    to network availability, and (3) a stale-but-present token still tells us
    the operator intended to enable user-scoped tools. If the token turns out to
    be invalid, the call-time error is more useful than refusing to register.
    """
    return bool(DIGIKEY_REFRESH_TOKEN_SEED) or DIGIKEY_TOKEN_CACHE.exists()


_USER_AUTH_AVAILABLE = _user_auth_available()
if _USER_AUTH_AVAILABLE:
    logger.info(
        "User-scoped tools enabled (refresh token: %s).",
        "from cache" if DIGIKEY_TOKEN_CACHE.exists() else "from seed env var",
    )
else:
    logger.info(
        "User-scoped tools NOT registered — no refresh token at %s and "
        "DIGIKEY_REFRESH_TOKEN_SEED is unset. Run `digikey-mcp-auth login` to enable.",
        DIGIKEY_TOKEN_CACHE,
    )


def _user_scoped_tool(fn):
    """Decorator: equivalent to @mcp.tool() when user-scoped auth is configured,
    no-op otherwise. The plain function stays accessible under its name either
    way — that lets tests and direct-import callers exercise the logic without
    requiring tool registration."""
    if _USER_AUTH_AVAILABLE:
        return mcp.tool()(fn)
    return fn


# -----------------------------------------------------------------------------
# MyLists v1 — saved BOM / parts lists tied to a DigiKey customer account.
# See _get_user_access_token() and the digikey_mcp_auth helper for the
# bootstrap flow.
# -----------------------------------------------------------------------------


def _slim_list_data(ld: dict) -> dict:
    """Trim a MyLists ListData entry to the fields useful in an LLM context.

    Drops the full PartsList (which can blow context on a large list — call
    get_parts_in_list explicitly for that), nested user info, and the column-
    preferences UI metadata.
    """
    return {
        "Id": ld.get("Id"),
        "ListName": ld.get("ListName"),
        "TotalParts": ld.get("TotalParts"),
        "CreatedBy": ld.get("CreatedBy"),
        "DateCreated": ld.get("DateCreated"),
        "DateModified": ld.get("DateModified"),
        "DateLastAccessed": ld.get("DateLastAccessed"),
        "Notes": ld.get("Notes"),
        "Tags": ld.get("Tags"),
        "CanEdit": ld.get("CanEdit"),
    }


def _slim_list_part(p: dict) -> dict:
    """Trim a MyLists ListPart to the fields useful in an LLM context.

    Drops display-only flags (ListPartFlags), substitute/alternate arrays (call
    search_product_substitutions for those), category-of-origin info, and the
    full quantity break-pricing matrix. Keeps identifiers, the user's annotations
    (CustomerReference, ReferenceDesignator, Notes), and the canonical part
    fields needed to round-trip a list item back to product details.
    """
    return {
        "UniqueId": p.get("UniqueId"),
        "RequestedPartNumber": p.get("RequestedPartNumber"),
        "DigiKeyPartNumber": p.get("DigiKeyPartNumber"),
        "ManufacturerPartNumber": p.get("ManufacturerPartNumber"),
        "Manufacturer": p.get("Manufacturer"),
        "Description": p.get("Description"),
        "CustomerReference": p.get("CustomerReference"),
        "ReferenceDesignator": p.get("ReferenceDesignator"),
        "Notes": p.get("Notes"),
        "QuantityAvailable": p.get("QuantityAvailable"),
        "PartStatus": p.get("PartStatus"),
        "PartDetailUrl": p.get("PartDetailUrl"),
        # The requested quantity is buried under Quantities[SelectedQuantityIndex].
        # Surface just the selected one — callers writing back via update_part_in_list
        # only need this scalar.
        "RequestedQuantity": _selected_quantity(p),
    }


def _selected_quantity(p: dict) -> int | None:
    quantities = p.get("Quantities") or []
    idx = p.get("SelectedQuantityIndex") or 0
    if 0 <= idx < len(quantities):
        return quantities[idx].get("QuantityRequested")
    return None


@_user_scoped_tool
def list_my_lists(start_index: int = 0, limit: int = 50):
    """List the user's saved DigiKey MyLists (BOM / parts lists).

    Returns slim list metadata (ID, name, part count, dates). Use get_my_list or
    get_parts_in_list to fetch the contents of a specific list.

    Args:
        start_index: Pagination offset. Default 0.
        limit: Max lists to return. Default 50, matches DigiKey's default.
    """
    raw = _make_user_request(
        "GET", f"{MYLISTS_BASE}/lists",
        params={"startIndex": start_index, "limit": limit},
    )
    return [_slim_list_data(ld) for ld in (raw or [])]


@_user_scoped_tool
def get_my_list(list_id: str):
    """Get metadata for a specific MyList (name, dates, part count, tags, notes).

    This does NOT return the parts themselves — DigiKey's GET /lists/{id} returns
    an empty PartsList field even though the schema declares it. Call
    get_parts_in_list(list_id) to fetch the actual parts.

    Args:
        list_id: The DigiKey list ID (from list_my_lists).
    """
    raw = _make_user_request("GET", f"{MYLISTS_BASE}/lists/{list_id}")
    return _slim_list_data(raw) if raw else None


@_user_scoped_tool
def create_my_list(list_name: str, notes: str = None, tags: list = None):
    """Create a new MyList. Returns the new list ID (as a string).

    Args:
        list_name: Display name for the new list. Must be unique for the user — use
            validate_my_list_name first if you want to check before creating.
        notes: Optional free-form notes attached to the list.
        tags: Optional list of tag strings.
    """
    body = {"ListName": list_name}
    if notes is not None:
        body["Notes"] = notes
    if tags:
        body["Tags"] = tags
    return _make_user_request("POST", f"{MYLISTS_BASE}/lists", data=body)


@_user_scoped_tool
def delete_my_list(list_id: str):
    """Delete a MyList permanently. Cannot be undone.

    Args:
        list_id: The list to delete.
    """
    _make_user_request("DELETE", f"{MYLISTS_BASE}/lists/{list_id}")
    return {"deleted": list_id}


@_user_scoped_tool
def update_my_list_name(list_id: str, new_name: str):
    """Rename an existing MyList.

    Args:
        list_id: The list to rename.
        new_name: The new display name. Must be unique for the user.
    """
    # quote(safe="") percent-encodes everything except unreserved chars — names
    # containing '/', '?', '#', or '%' get encoded rather than reinterpreted as
    # URL syntax. Same applies to validate_my_list_name below.
    _make_user_request(
        "PUT",
        f"{MYLISTS_BASE}/lists/{list_id}/listName/{urllib.parse.quote(new_name, safe='')}",
    )
    return {"id": list_id, "list_name": new_name}


@_user_scoped_tool
def validate_my_list_name(list_name: str):
    """Check whether a list name is available (not already used by the user).

    Args:
        list_name: Candidate name.
    """
    return _make_user_request(
        "GET",
        f"{MYLISTS_BASE}/lists/validate/{urllib.parse.quote(list_name, safe='')}",
    )


@_user_scoped_tool
def get_parts_in_list(list_id: str, start_index: int = 0, limit: int = 50):
    """Get parts in a MyList (paginated). Returns slim parts.

    Use this instead of get_my_list when the list is large or you only need parts.

    Args:
        list_id: The list to read.
        start_index: Pagination offset.
        limit: Max parts to return.
    """
    raw = _make_user_request(
        "GET", f"{MYLISTS_BASE}/lists/{list_id}/parts",
        params={"startIndex": start_index, "limit": limit},
    )
    if not raw:
        return {"TotalParts": 0, "PartsList": []}
    return {
        "TotalParts": raw.get("TotalParts"),
        "PartsList": [_slim_list_part(p) for p in (raw.get("PartsList") or [])],
    }


# DigiKey's per-part GET requires locale query params even though the swagger marks
# them optional — without them the API returns 400 "request not formatted acceptably".
# These match the locale headers _get_user_headers already sends and rarely need to
# vary, so we don't expose them as tool args.
_LOCALE_PART_PARAMS = {"countryIso": "US", "currencyIso": "USD", "languageIso": "en"}


@_user_scoped_tool
def get_part_from_list(list_id: str, unique_id: str):
    """Get a single part from a MyList by its UniqueId.

    Args:
        list_id: The list ID.
        unique_id: The part's UniqueId within the list (from get_parts_in_list).
    """
    raw = _make_user_request(
        "GET", f"{MYLISTS_BASE}/lists/{list_id}/parts/{unique_id}",
        params=_LOCALE_PART_PARAMS,
    )
    return _slim_list_part(raw) if raw else None


def _build_requested_part(
    part_number: str,
    quantity: int,
    customer_reference: str = None,
    reference_designator: str = None,
    notes: str = None,
) -> dict:
    """Construct a RequestedPart body from a convenience-shaped input.

    Quantity goes into the Quantities array because that's how DigiKey nests it.
    SelectedQuantityIndex defaults to 0 (the only entry we send).
    """
    body = {
        "RequestedPartNumber": part_number,
        "Quantities": [{"Quantity": int(quantity)}],
        "SelectedQuantityIndex": 0,
    }
    if customer_reference is not None:
        body["CustomerReference"] = customer_reference
    if reference_designator is not None:
        body["ReferenceDesignator"] = reference_designator
    if notes is not None:
        body["Notes"] = notes
    return body


@_user_scoped_tool
def add_parts_to_list(list_id: str, parts: list, index: int = 0):
    """Add one or more parts to a MyList.

    Each part is a dict with at minimum {"part_number": "...", "quantity": N}.
    Optional fields: customer_reference, reference_designator, notes.

    Example:
        add_parts_to_list(
            list_id="abc-123",
            parts=[
                {"part_number": "565-1571-1-ND", "quantity": 10, "reference_designator": "C1"},
                {"part_number": "1276-1010-1-ND", "quantity": 5},
            ],
        )

    Returns a list of the UniqueIds DigiKey assigned to the new entries.

    Args:
        list_id: The target list.
        parts: List of {part_number, quantity, ...} dicts.
        index: Insert position within the list. Default 0 (top).
    """
    if not parts:
        raise ValueError("parts must be a non-empty list")
    body = []
    for p in parts:
        if not isinstance(p, dict) or "part_number" not in p or "quantity" not in p:
            raise ValueError(
                "Each entry in parts must be a dict with at least "
                f"'part_number' and 'quantity'. Got: {p!r}"
            )
        body.append(_build_requested_part(
            part_number=p["part_number"],
            quantity=p["quantity"],
            customer_reference=p.get("customer_reference"),
            reference_designator=p.get("reference_designator"),
            notes=p.get("notes"),
        ))
    return _make_user_request(
        "POST", f"{MYLISTS_BASE}/lists/{list_id}/parts",
        data=body, params={"index": index},
    )


@_user_scoped_tool
def update_part_in_list(
    list_id: str,
    unique_id: str,
    quantity: int = None,
    customer_reference: str = None,
    reference_designator: str = None,
    notes: str = None,
):
    """Update fields on an existing part in a MyList. Only fields you pass are
    changed; omitted args leave the existing value in place.

    Args:
        list_id: The list ID.
        unique_id: The part's UniqueId within the list.
        quantity: New quantity. Omit to leave unchanged.
        customer_reference: New customer reference. Omit to leave unchanged.
        reference_designator: New reference designator. Omit to leave unchanged.
        notes: New notes. Omit to leave unchanged.
    """
    # DigiKey's PUT replaces the whole RequestedPart object, so we need the current
    # values to preserve fields the caller didn't touch.
    current_raw = _make_user_request(
        "GET", f"{MYLISTS_BASE}/lists/{list_id}/parts/{unique_id}",
        params=_LOCALE_PART_PARAMS,
    )
    if not current_raw:
        raise ValueError(f"Part {unique_id!r} not found in list {list_id!r}")

    # GET returns ListPartQuantity (QuantityRequested, PackOptions, …); PUT
    # expects RequestedQuantity (Quantity, TargetPrice, SelectedPackType,
    # SelectedSubPackType). A list part is one quantity in practice — the
    # multi-entry case in the GET shape is DigiKey's pack-option state, not
    # alternate quantities. Build one PUT-shape entry from the selected GET
    # entry, with the new quantity if the caller provided one.
    existing = current_raw.get("Quantities") or []
    selected_idx = current_raw.get("SelectedQuantityIndex") or 0
    src = existing[selected_idx] if 0 <= selected_idx < len(existing) else {}

    put_quantity = {
        "Quantity": int(quantity) if quantity is not None else int(src.get("QuantityRequested") or 1),
    }
    # Only echo back fields the GET actually had. Drop both None and "" — DigiKey
    # uses "" as a sentinel for "no preference" on pack types, and echoing it
    # back serves no purpose vs just omitting the key.
    for k in ("TargetPrice", "SelectedPackType", "SelectedSubPackType"):
        v = src.get(k)
        if v:
            put_quantity[k] = v

    body = {
        "UniqueId": current_raw.get("UniqueId"),
        "PartId": current_raw.get("PartId"),
        "RequestedPartNumber": current_raw.get("RequestedPartNumber"),
        "OriginalPartNumber": current_raw.get("OriginalPartNumber"),
        "ManufacturerName": current_raw.get("RequestedManufacturerName") or current_raw.get("Manufacturer"),
        "CustomerReference": customer_reference if customer_reference is not None else current_raw.get("CustomerReference"),
        "ReferenceDesignator": reference_designator if reference_designator is not None else current_raw.get("ReferenceDesignator"),
        "Notes": notes if notes is not None else current_raw.get("Notes"),
        "SelectedQuantityIndex": 0,
        "Quantities": [put_quantity],
    }

    _make_user_request(
        "PUT", f"{MYLISTS_BASE}/lists/{list_id}/parts/{unique_id}",
        data=body,
    )
    return {"updated": unique_id}


@_user_scoped_tool
def delete_part_from_list(list_id: str, unique_id: str):
    """Remove a part from a MyList by its UniqueId.

    Args:
        list_id: The list ID.
        unique_id: The part's UniqueId within the list.
    """
    _make_user_request("DELETE", f"{MYLISTS_BASE}/lists/{list_id}/parts/{unique_id}")
    return {"deleted": unique_id}


def main():
    """Entry point.

    Transport selection comes from DIGIKEY_MCP_TRANSPORT:
      * stdio (default)         — for local MCP clients spawning this as a child
                                   process. Matches what existing users get.
      * http / streamable-http  — FastMCP's HTTP transport; binds host/port from
                                   DIGIKEY_MCP_HOST / DIGIKEY_MCP_PORT.

    Defaults are deliberate: stdio so existing setups don't break, and when http
    is requested the bind defaults to 127.0.0.1 — containers that want to expose
    the port set DIGIKEY_MCP_HOST=0.0.0.0 explicitly (the Dockerfile does this).
    """
    transport = os.getenv("DIGIKEY_MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport in ("http", "streamable-http", "sse"):
        host = os.getenv("DIGIKEY_MCP_HOST", "127.0.0.1")
        port = int(os.getenv("DIGIKEY_MCP_PORT", "8000"))
        logger.info("Starting %s transport on %s:%s", transport, host, port)
        mcp.run(transport=transport, host=host, port=port)
        return
    raise ValueError(
        f"Unknown DIGIKEY_MCP_TRANSPORT={transport!r}. "
        f"Valid values: stdio, http, streamable-http, sse."
    )


if __name__ == "__main__":
    main()