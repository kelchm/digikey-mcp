# DigiKey MCP Server

An MCP server for DigiKey's APIs, built on FastMCP. Covers Product Search v4 (parametric component search by attribute name and value, with cross-unit range support — `find_components` is the headline tool) and MyLists v1 (saved BOM / parts list CRUD). MyLists is user-scoped — it uses 3-legged OAuth and is the first of a category of "user-scoped" DigiKey APIs we may extend the server to cover (Orders, Cart, etc.). See [user-scoped tool setup](#user-scoped-tool-setup) for the bootstrap.

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
```dotenv
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

By default the server uses MCP's stdio transport. To run as an HTTP server (for remote MCP clients, sidecar deployments, etc.):

```bash
DIGIKEY_MCP_TRANSPORT=http \
DIGIKEY_MCP_HOST=127.0.0.1 \
DIGIKEY_MCP_PORT=8000 \
uv run digikey-mcp
# MCP endpoint: http://127.0.0.1:8000/mcp/
```

`DIGIKEY_MCP_HOST` defaults to `127.0.0.1` (loopback only). For container deployments that expose the port, set `DIGIKEY_MCP_HOST=0.0.0.0` — the bundled Dockerfile does this.

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

### User-scoped tools (require 3-legged OAuth — see [user-scoped tool setup](#user-scoped-tool-setup))

> **Conditional registration.** Tools that require user-context auth only appear in the MCP tool list when that auth is plausibly configured — that is, `DIGIKEY_REFRESH_TOKEN_SEED` is set OR a token cache file exists at `DIGIKEY_TOKEN_CACHE` at server startup. On a deployment without user auth, an agent connecting via this MCP sees the Product Search tools only. Adding user auth post-startup requires a server restart. MyLists is the first user-scoped surface; Orders / Cart / etc. would share the same gate when added.

#### MyLists v1

- `list_my_lists(start_index=0, limit=50)` — saved BOM / parts lists.
- `get_my_list(list_id)` — list metadata + parts.
- `create_my_list(list_name, notes=None, tags=None)` — returns the new list ID.
- `delete_my_list(list_id)`, `update_my_list_name(list_id, new_name)`.
- `validate_my_list_name(list_name)` — name-availability boolean. (DigiKey's swagger documents a sibling `/validate/name/{listName}` "suggest a variant" endpoint, but the deployed API 404s on it, so it's not exposed.)
- `get_parts_in_list(list_id, start_index=0, limit=50)`, `get_part_from_list(list_id, unique_id)`.
- `add_parts_to_list(list_id, parts, index=0)` — `parts` is a list of `{"part_number", "quantity", "customer_reference"?, "reference_designator"?, "notes"?}` dicts.
- `update_part_in_list(list_id, unique_id, quantity=None, customer_reference=None, reference_designator=None, notes=None)` — only fields you pass are changed.
- `delete_part_from_list(list_id, unique_id)`.

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

## User-scoped tool setup

Some DigiKey APIs (MyLists today; Orders / Cart in future) require **3-legged OAuth** — the per-customer-account authorization-code flow, not the `client_credentials` grant the Product Search tools use. The Product Search tools keep working with just `CLIENT_ID` / `CLIENT_SECRET`; user-scoped tools need one extra one-time setup step, and don't register at all when that step hasn't been completed (see [Conditional registration](#user-scoped-tools-require-3-legged-oauth--see-user-scoped-tool-setup) above).

### One-time bootstrap (run locally)

You need an HTTPS redirect URI registered on your DigiKey app — `https://localhost` is the simplest choice and is what DigiKey suggests for apps without existing callback infrastructure. DigiKey rejects plain `http://...` redirect URIs.

```bash
uvx --from . digikey-mcp-auth login
# Prints a long authorize URL; opens it in your browser. Log in to DigiKey.
# Your browser will redirect to https://localhost and likely show a "this site
# can't be reached" error — that's fine. Copy the full URL from the address
# bar (it contains ?code=...) and paste it back into the CLI.
# The CLI prints a refresh_token.
```

### Local dev: write directly to the cache

```bash
uvx --from . digikey-mcp-auth login --write-cache
# Tokens saved to $XDG_CONFIG_HOME/digikey-mcp/tokens.json (default
# ~/.config/digikey-mcp/tokens.json), mode 0600. Start the server normally —
# it picks up the file automatically.
```

### Remote deployment: seed via env var

DigiKey **rotates the refresh token on every refresh** — yesterday's token is invalid the moment a new one is issued. That means a plain env var goes stale immediately. The server bootstraps from `DIGIKEY_REFRESH_TOKEN_SEED` once, then persists the rotating token to a writable cache file at `DIGIKEY_TOKEN_CACHE`:

```bash
# In the deployment's env (e.g. mcpjungle config, sidecar container env, etc.):
CLIENT_ID=...
CLIENT_SECRET=...
DIGIKEY_REFRESH_TOKEN_SEED=<value printed by digikey-mcp-auth login>
DIGIKEY_TOKEN_CACHE=/data/digikey-mcp/tokens.json    # any writable file path
DIGIKEY_ACCOUNT_ID=...                                # optional; X-DIGIKEY-Account-Id
DIGIKEY_REDIRECT_URI=https://localhost                # must match what you used during login
```

Mount a small writable volume at the cache path. On the first user-scoped tool call, the server consumes the seed, refreshes against DigiKey, writes the rotated tokens to the cache, and ignores the seed forever after.

**If the cache path isn't writable**, the server falls back to in-memory tokens for the life of the process and logs a warning. That works until the process restarts, at which point it needs a fresh seed. mcpjungle's default deployment doesn't expose writable mounts to child MCP processes, so you'll either want to switch to a sidecar container with its own volume or accept the restart-requires-reseed tradeoff.

### Refresh-token lifecycle

- **Access token**: ~30 min lifetime; refreshed silently when within 60 s of expiry.
- **Refresh token**: doesn't expire on a timer, but rotates on every use. The cache file is the only place the current valid one lives — don't try to keep a separate copy.
- **If you see `invalid_grant`**: someone or something else used the cached refresh token (process restart against a stale seed, manual logout, two deployments sharing a cache, etc.). Re-run `digikey-mcp-auth login` and re-seed.

### What MyLists calls return

Tools follow the same slim-shape conventions as `find_components` — PascalCase passthrough where the field shape is unchanged, distinct names where we collapse arrays into scalars (e.g. `RequestedQuantity` is the selected `Quantities[i].QuantityRequested`). `get_my_list` and `get_parts_in_list` drop the heavy `Flags`, `Substitutes`, `AlternateParts`, and pricing-break arrays — call `get_product_pricing` / `search_product_substitutions` if you need them.

## Container deployment

A Dockerfile is included. Image defaults to HTTP transport on `0.0.0.0:8000` with the token cache at `/data/tokens.json` (declared as a volume so a host bind-mount works without extra chown).

```bash
docker build -t digikey-mcp .

docker run -d \
  --name digikey-mcp \
  -p 8000:8000 \
  -e CLIENT_ID=... \
  -e CLIENT_SECRET=... \
  -e DIGIKEY_REFRESH_TOKEN_SEED=... \
  -v digikey-mcp-data:/data \
  digikey-mcp
# MCP endpoint: http://<host>:8000/mcp/
```

To run as stdio under a process supervisor (mcpjungle, etc.) instead:

```bash
docker run --rm -i \
  -e DIGIKEY_MCP_TRANSPORT=stdio \
  -e CLIENT_ID=... -e CLIENT_SECRET=... \
  digikey-mcp
```

### Published images

Tagged commits (`v*`) trigger a GitHub Actions workflow that builds linux/amd64 + linux/arm64 and publishes to GHCR:

```bash
docker pull ghcr.io/kelchm/digikey-mcp:latest
# or pin: ghcr.io/kelchm/digikey-mcp:1.2.3   |   ghcr.io/kelchm/digikey-mcp:1.2
```

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
