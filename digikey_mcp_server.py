import os
import re
import json
import logging
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
    API_BASE = "https://sandbox-api.digikey.com"
else:
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    API_BASE = "https://api.digikey.com"

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
    resp = requests.post(TOKEN_URL, data=data, headers=headers)
    
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
        resp = requests.get(url, headers=headers)
    else:
        resp = requests.post(url, headers=headers, json=data)
    
    logger.info(f"Response status: {resp.status_code}")
    if resp.status_code != 200:
        logger.error(f"API error: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
    
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
        search_options: Comma-delimited filters like LeadFree,RoHSCompliant,InStock
        sort_field: Field to sort by. Options: None, Packaging, ProductStatus, DigiKeyProductNumber, ManufacturerProductNumber, Manufacturer, MinimumQuantity, QuantityAvailable, Price, Supplier, PriceManufacturerStandardPackage
        sort_order: Sort direction - Ascending or Descending (default: Ascending)
    """
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
    raise ValueError(f"Attribute name '{name}' not found in category. Available: {all_names}")


_UREG = pint.UnitRegistry()


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
        match = exact[0] if len(exact) == 1 else None
        if not match:
            contains = [fv for fv in available_values if target and target in _normalize_text(fv.get("ValueName"))]
            if len(contains) == 1:
                match = contains[0]
            elif len(contains) > 1:
                names = [fv.get("ValueName") for fv in contains]
                raise ValueError(f"Value '{v}' for '{param.get('ParameterName')}' is ambiguous: {names}")
        if not match:
            sample = [fv.get("ValueName") for fv in available_values[:20]]
            more = "" if len(available_values) <= 20 else f" (+{len(available_values)-20} more)"
            raise ValueError(f"Value '{v}' not found for '{param.get('ParameterName')}'. Available: {sample}{more}")
        resolved.append(match.get("ValueId"))
    return resolved


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
    filters_meta = _get_parametric_filters(category_id=category_id, keywords="", limit=1)

    # Broad parent categories (e.g. cat 20 Connectors, cat 32 Integrated Circuits) return
    # no parametric filters — DigiKey only computes facets at the leaf level. The error you'd
    # get later from _match_parameter is confusing ("Available: []") because it points at the
    # attribute name when the real problem is the category. Catch it up front when the caller
    # actually asked for attribute filtering.
    if attributes and not filters_meta:
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
            applied[param.get("ParameterName")] = {
                "MatchedCount": len(matched_names),
                "From": matched_names[0] if matched_names else None,
                "To": matched_names[-1] if matched_names else None,
                "Sample": matched_names[:5],
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


def main():
    mcp.run()

if __name__ == "__main__":
    main() 