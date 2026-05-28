# DigiKey MCP Server

A Model Context Protocol (MCP) server for DigiKey's Product Search API using FastMCP.

## Requirements

- Python 3.10+
- uv package manager
- DigiKey API credentials (CLIENT_ID and CLIENT_SECRET)

## Setup

### 1. Install dependencies
```bash
uv sync
```

### 2. Set up environment variables
Create a `.env` file in the project root:
```
CLIENT_ID=your_digikey_client_id
CLIENT_SECRET=your_digikey_client_secret
USE_SANDBOX=false
```

Leave `USE_SANDBOX=false` (or omit it) for normal use. DigiKey's sandbox Product Search
returns a single canned example product regardless of query, per their
[FAQ](https://developer.digikey.com/faq) — it's useful only for OAuth/connectivity testing
and will be silently misleading for real searches.

### 3. Run the server
```bash
uv run python digikey_mcp_server.py
```

### 4. Tests
The test suite runs offline against captured API snapshots — no credentials needed,
no quota consumed:
```bash
uv run pytest
```
To refresh the snapshots against the live API (requires valid `.env`):
```bash
uv run python tests/refresh_snapshots.py
```
The refresh script consumes a handful of API calls and writes JSON fixtures into
`tests/fixtures/`. Test scenarios live in `tests/test_parametric_search.py`; the
matching capture scenarios live in `tests/refresh_snapshots.py`. Add a new scenario
to both and re-run refresh to extend coverage.

## Available Tools

### Search Methods
- `find_components(category_id, attributes=None, keywords="", limit=25, sort_by_attribute=None, sort_order="Ascending", in_stock_only=False)` - Parametric search by attribute (capacitance, diameter, etc.). Takes human-readable names/values, resolves them to DigiKey ids internally, returns slim results, and can sort client-side by any parametric attribute (e.g. "rated life").
- `keyword_search(keywords, limit=5, manufacturer_id=None, category_id=None, search_options=None, sort_field=None, sort_order="Ascending")` - Free-text search by keyword or part number. For attribute-based queries, use `find_components`.
- `get_parametric_filters(category_id, keywords="", limit=1)` - List the available parametric attributes and values for a category (used internally by `find_components`; useful for advanced callers who want to inspect the available filters).
- `search_manufacturers()` - Get all product manufacturers
- `search_categories()` - Get all product categories
- `search_product_substitutions(product_number, limit=10, search_options=None, exclude_marketplace=False)` - Find substitute products

### Product Details
- `product_details(product_number, manufacturer_id=None, customer_id="0")` - Get detailed product information
- `get_category_by_id(category_id)` - Get specific category details
- `get_product_media(product_number)` - Get product images, documents, and videos
- `get_product_pricing(product_number, customer_id="0", requested_quantity=1)` - Get detailed pricing information
- `get_digi_reel_pricing(product_number, requested_quantity, customer_id="0")` - Get DigiReel pricing

### Sort Options for keyword_search
Available sort fields:
- `Packaging` - Sort by packaging type
- `ProductStatus` - Sort by product status
- `DigiKeyProductNumber` - Sort by DigiKey part number
- `ManufacturerProductNumber` - Sort by manufacturer part number
- `Manufacturer` - Sort by manufacturer name
- `MinimumQuantity` - Sort by minimum order quantity
- `QuantityAvailable` - Sort by available quantity
- `Price` - Sort by price
- `Supplier` - Sort by supplier
- `PriceManufacturerStandardPackage` - Sort by manufacturer standard package price

Sort orders: `Ascending` or `Descending`

### Search Options
Available filters for search methods:
- `LeadFree` - Lead-free products only
- `RoHSCompliant` - RoHS compliant products only
- `InStock` - In-stock products only
- `HasDatasheet` - Products with datasheets
- `HasProductPhoto` - Products with photos
- `Has3DModel` - Products with 3D models
- `NewProduct` - New products only

## Example Usage

The server exposes MCP tools that can be used by MCP clients like Claude Desktop, or programmatically via FastMCP clients.

### Search Examples
```python
# Basic keyword search
keyword_search("resistor", limit=10)

# Search with sorting by price (lowest first)
keyword_search("capacitor", limit=5, sort_field="Price", sort_order="Ascending")

# Search with filters
keyword_search("LED", limit=10, search_options="InStock,RoHSCompliant")

# Get product details
product_details("296-8875-1-ND")

# Get pricing for specific quantity
get_product_pricing("296-8875-1-ND", requested_quantity=100)
```

### Parametric Search

Use `find_components` to constrain results by attribute (capacitance, diameter, lifetime, etc.).
Pass human-readable attribute names and values; the tool resolves them to DigiKey ids internally
and returns slim results.

**Discrete match:**
```python
find_components(
    category_id="58",  # Aluminum Electrolytic Capacitors
    attributes={"Capacitance": "470 µF"},
    sort_by_attribute="Lifetime @ Temp.",
    sort_order="Descending",
    in_stock_only=True,
)
```

**Range match** — pass `{"min": ..., "max": ...}` (either bound optional):
```python
find_components(
    category_id="58",
    attributes={
        "Capacitance": {"min": "100 µF", "max": "1000 µF"},
        "Diameter - Seated (Max)": {"max": "10mm"},
    },
)
```

Ranges work for any parameter whose values are clean unit-bearing quantities (Capacitance,
Voltage, Resistance, Dimensions, etc.). Unit parsing is delegated to [pint](https://pint.readthedocs.io/),
so bounds and histogram values can use *different* units in the same family — `{"min": "0.5 mF"}`
will correctly match histogram values stored as `"500 µF"` and above.

For parameters with non-quantity values (e.g. `"8000 Hrs @ 105°C"` for lifetime, `"±20%"` for
tolerance), range matching errors out with sample values; use discrete value matching or a
list of values instead.

If an attribute name or value doesn't match, the error message lists close candidates so you can retry.

#### How it works (the trick)

The DigiKey v4 keyword-search endpoint can't do a true category browse — empty `Keywords` returns
a 400 and `"*"` is treated as a literal-character match, yielding only a handful of products with
a sparse 1–3-bucket facet histogram. The workaround: **use the category's own name as the
`Keywords` value**. That's a high-recall match that broadly hits everything in the category, while
`CategoryFilter` scopes the results to the leaf. `find_components` does this automatically by
looking up the category name via `/categories/{id}` (cached per process).

#### Notes / limits

- `category_id` is required (parameters are category-scoped — find it via `search_categories`).
- The DigiKey API does not sort by parametric attributes — `find_components` does this client-side
  *after* the fetch, so very large result sets need a higher `limit` to sort over a meaningful slice.
- `get_parametric_filters(category_id)` is exposed as an escape hatch for advanced callers who want
  to inspect the available parameter names and value histograms before composing a query.

## Claude Desktop Integration

Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "digikey": {
      "command": "uv",
      "args": ["run", "python", "digikey_mcp_server.py"],
      "cwd": "/path/to/project"
    }
  }
}
``` 