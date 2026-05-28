# DigiKey MCP Server

An MCP server for DigiKey's Product Search v4 API, built on FastMCP. The main tool is `find_components`: parametric component search by attribute name and value, with cross-unit range support.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- DigiKey API credentials (`CLIENT_ID` and `CLIENT_SECRET`)

## Setup

### 1. Install dependencies
```bash
uv sync
```

### 2. Environment variables
Create `.env` in the project root:
```
CLIENT_ID=your_digikey_client_id
CLIENT_SECRET=your_digikey_client_secret
```

Optional:
- `USE_SANDBOX=true` — DigiKey's sandbox returns a single canned product regardless of query (per their [FAQ](https://developer.digikey.com/faq)). Use only for OAuth/connectivity testing; never for real searches.
- `DIGIKEY_OFFLINE_MODE=true` — skips OAuth at module import. Used by the offline test suite.

### 3. Run the server
```bash
uv run python digikey_mcp_server.py
# or, after the package installs:
uv run digikey-mcp
```

### 4. Tests
The test suite runs offline against captured API snapshots — no credentials, no quota:
```bash
uv run pytest
```
To refresh the snapshots against the live API (requires valid `.env`):
```bash
uv run python tests/refresh_snapshots.py
```
To add a new scenario, add the capture call to `tests/refresh_snapshots.py` and the assertion to `tests/test_parametric_search.py`, then re-run refresh.

## Tools

### Parametric search

- **`find_components(category_id, attributes=None, keywords="", limit=25, in_stock_only=False)`** — search by attribute name and value. Returns slim products. Discrete, list, and range inputs all supported.
- **`get_parametric_filters(category_id, parameter_name=None, max_values=100, keywords="")`** — list the attributes available for a category. Returns a summary by default; pass `parameter_name="..."` to get the values for one specific parameter.

### Free-text and part lookups
- `keyword_search(keywords, limit=5, manufacturer_id=None, category_id=None, search_options=None, sort_field=None, sort_order="Ascending")` — full-text search or part-number lookup. Returns DigiKey's raw response shape.
- `product_details(product_number, manufacturer_id=None, customer_id="0")` — full product detail for a known part.
- `search_product_substitutions(product_number, limit=10, search_options=None, exclude_marketplace=False)` — substitutes for a given part.

### Reference data
- `search_manufacturers()` — full manufacturer list (IDs usable as `manufacturer_id` elsewhere).
- `search_categories()` — full category tree.
- `get_category_by_id(category_id)` — single category detail.

### Pricing and media
- `get_product_pricing(product_number, customer_id="0", requested_quantity=1)` — full price tiers.
- `get_digi_reel_pricing(product_number, requested_quantity, customer_id="0")` — DigiReel pricing.
- `get_product_media(product_number)` — images, datasheets, videos.

## Parametric search guide

### Discrete value
```python
find_components(
    category_id="58",  # Aluminum Electrolytic Capacitors
    attributes={"Capacitance": "470 µF"},
    in_stock_only=True,
)
```

### List of values (match any)
```python
find_components(
    category_id="58",
    attributes={"Capacitance": ["100 µF", "470 µF", "1000 µF"]},
)
```

### Range — `{min, max}` (either bound optional)
```python
find_components(
    category_id="58",
    attributes={
        "Capacitance": {"min": "100 µF", "max": "1000 µF"},
        "Diameter - Seated (Max)": {"max": "10mm"},
    },
)
```

### Cross-unit ranges
Bounds and histogram values can use different units in the same family. `{"min": "0.5 mF"}` matches histogram values stored as `"500 µF"` and above. Unit parsing uses [pint](https://pint.readthedocs.io/).

### Magnitude-alias expansion
DigiKey stores the same physical value under multiple unit strings as separate histogram buckets — `"1 mF"` and `"1000 µF"` are two different entries. When you pass a discrete value, the tool finds every magnitude-equivalent ValueId and sends the union to DigiKey. That way you don't lose products tagged only under the other alias. `AppliedFilters` shows the expansion so you can see what was sent.

### Coupled-unit parameters
Ranges don't work on `CoupledUnitOfMeasure` parameters like `Ripple Current @ Low Frequency`, where values look like `"500 mA @ 100 kHz"`. The tool rejects them with an error — two axes can't collapse to a single number you can compare. Use discrete or list values instead.

### Parent categories
Parent categories like `20` (Connectors) or `32` (ICs) return no parametric filters; DigiKey only computes facets at leaf categories. `find_components` raises a clear error telling you to find a leaf subcategory via `search_categories`.

### Error messages
When a name or value doesn't resolve, the error includes a `Did you mean: [...]` list. Attribute names are ranked by edit distance. Values are ranked by magnitude proximity if pint can parse them (`"473 µF"` suggests `["470 µF", "480 µF", ...]`), otherwise by edit distance.

### Why there's no parametric sort
DigiKey's API can't sort by parametric attributes server-side. Sorting a returned page client-side would be misleading — you'd be re-ordering N results out of a much larger match set, not finding the actual top-N. If you want the top-K by some attribute, narrow with parametric filters until the result fits one page, then sort `Products[]` yourself.

## Response shapes

### `find_components` returns
```python
{
    "ProductsCount": int,        # total matches in DigiKey (may exceed len(Products))
    "AppliedFilters": {
        # Shape per attribute depends on what you passed in:
        "Capacitance": ["470 µF", ...],     # discrete/list input → list of matched names
        "Capacitance": {                     # range input → summary
            "MatchedCount": 45,              # distinct physical magnitudes (aliases collapsed)
            "From": "100 µF",                # most popular alias of the lowest match
            "To": "470 µF",                  # most popular alias of the highest match
            "Sample": ["100 µF", ...],       # first 5 by popularity, no alias dupes
        },
    },
    "Products": [slim_product, ...]
}
```

### Slim product
Field names are PascalCase. Anything that passes through from DigiKey unchanged keeps its original name. Where we transformed the shape (object → string, list → flat dict), the field gets a different name so it doesn't collide with DigiKey's:

```python
{
    "ManufacturerProductNumber": "...",   # straight passthrough
    "DigiKeyProductNumber": "...",        # primary variation's DK PN (DigiKey only exposes
                                          # this nested in ProductVariations; safe to reuse)
    "ManufacturerName": "...",            # was DigiKey's Manufacturer object {Id, Name}
    "ProductDescription": "...",          # was DigiKey's Description object
    "UnitPrice": float,                   # passthrough
    "QuantityAvailable": int,             # passthrough
    "DatasheetUrl": "...",                # passthrough
    "ProductUrl": "...",                  # passthrough
    "ParameterMap": {                     # was DigiKey's Parameters list-of-objects
        "Capacitance": "470 µF",
        "Voltage - Rated": "25 V",
        ...
    },
}
```

### `get_parametric_filters` — summary mode (default)
```python
[
    {"ParameterName": "Capacitance", "ParameterType": "UnitOfMeasure",
     "TotalCount": 685, "SampleValues": ["220 µF", "100 µF", "470 µF"]},
    {"ParameterName": "Voltage - Rated", "ParameterType": "UnitOfMeasure",
     "TotalCount": 81, "SampleValues": ["25 V", "16 V", "50 V"]},
    ...
]
```

### `get_parametric_filters` — drill-in mode (`parameter_name="..."`)
```python
{
    "ParameterId": 2049,
    "ParameterName": "Capacitance",
    "ParameterType": "UnitOfMeasure",
    "TotalCount": 685,           # total values in the histogram
    "Truncated": True,           # whether FilterValues is a subset
    "FilterValues": [            # top max_values by ProductCount
        {"ValueId": "220 µF", "ValueName": "220 µF",
         "ProductCount": 5761, "RangeFilterType": None},
        ...
    ],
}
```

`keyword_search` and the other tools return DigiKey's raw response shape — see the v4 swagger in `docs/digikey_product_search_v4_swagger.json` for the full schema.

## Notes on the v4 API

Two things to know if you're hacking on this.

**The category name has to be the keyword.** Empty `Keywords` returns a 400. `"*"` matches literally and gets you a sparse 1–3-bucket facet histogram. Sending the category's actual name (e.g. `"Aluminum Electrolytic Capacitors"`) with `CategoryFilter=58` is what produces the real per-category histogram — 685 capacitance values for cat 58 instead of 3. `find_components` does this lookup automatically and caches it.

**Filters must nest under `FilterOptionsRequest`.** Top-level `ManufacturerId`, `CategoryId`, and `SearchOptionList` are silently ignored — no error, just unfiltered results. They have to be nested as `FilterOptionsRequest.ManufacturerFilter`, `.CategoryFilter`, and `.SearchOptions`. The swagger schema doesn't make this jump out.

## Claude Desktop integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — two options:

**From a local checkout:**
```json
{
  "mcpServers": {
    "digikey": {
      "command": "uv",
      "args": ["run", "python", "digikey_mcp_server.py"],
      "cwd": "/path/to/digikey-mcp"
    }
  }
}
```

**Via `uvx` from this repo:**
```json
{
  "mcpServers": {
    "digikey": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/kelchm/digikey-mcp", "digikey-mcp"],
      "env": {
        "CLIENT_ID": "your_digikey_client_id",
        "CLIENT_SECRET": "your_digikey_client_secret"
      }
    }
  }
}
```
