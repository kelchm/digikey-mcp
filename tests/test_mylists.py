"""Offline tests for the MyLists v1 tool surface and the user-token refresh code.

These don't hit DigiKey at all — `_make_user_request` is patched at the test boundary
to return canned responses, and the refresh flow stubs out `requests.post`. The goal
is to cover the parts that have non-trivial logic (slimming, body construction,
read-modify-write on update_part_in_list, seed → cache bootstrap, refresh-token
rotation), not to fixture-capture every endpoint shape.
"""
import json
import time

import pytest

import digikey_mcp_server as srv

# Unwrap the FastMCP-decorated tools so we can call the underlying functions.
list_my_lists = srv.list_my_lists.fn
get_my_list = srv.get_my_list.fn
create_my_list = srv.create_my_list.fn
delete_my_list = srv.delete_my_list.fn
update_my_list_name = srv.update_my_list_name.fn
validate_my_list_name = srv.validate_my_list_name.fn
get_parts_in_list = srv.get_parts_in_list.fn
get_part_from_list = srv.get_part_from_list.fn
add_parts_to_list = srv.add_parts_to_list.fn
update_part_in_list = srv.update_part_in_list.fn
delete_part_from_list = srv.delete_part_from_list.fn


@pytest.fixture
def captured(monkeypatch):
    """Patch _make_user_request to record calls and return canned responses.

    The fixture returns a list of (method, url, data, params) tuples and a dict
    that tests pre-populate with response stubs keyed by (METHOD, URL_SUFFIX).
    """
    calls: list[tuple] = []
    stubs: dict[tuple, object] = {}

    def fake(method, url, data=None, params=None):
        calls.append((method, url, data, params))
        for (m, suffix), resp in stubs.items():
            if m == method and url.endswith(suffix):
                return resp
        return None

    monkeypatch.setattr(srv, "_make_user_request", fake)
    return {"calls": calls, "stubs": stubs}


# ---------- slimming / passthrough ----------

def test_list_my_lists_slims_metadata(captured):
    captured["stubs"][("GET", "/mylists/v1/lists")] = [
        {
            "Id": "list-abc",
            "ListName": "Power supply BOM",
            "TotalParts": 17,
            "CreatedBy": "kelchm@gmail.com",
            "DateCreated": "2026-05-01T12:00:00Z",
            "DateModified": "2026-05-15T09:00:00Z",
            "DateLastAccessed": "2026-05-28T22:00:00Z",
            "Notes": "rev B",
            "Tags": ["wip"],
            "CanEdit": True,
            # Noise that should be stripped:
            "PartsList": [{"junk": True} for _ in range(50)],
            "UserInformation": {"ContactId": 42},
            "ListSettings": {"Visibility": "private"},
            "Revisions": [{"Id": "r1"}],
        },
    ]
    out = list_my_lists()
    assert len(out) == 1
    item = out[0]
    assert item["Id"] == "list-abc"
    assert item["ListName"] == "Power supply BOM"
    assert item["TotalParts"] == 17
    # Slim form drops the heavy nested arrays:
    assert "PartsList" not in item
    assert "UserInformation" not in item
    assert "ListSettings" not in item


def test_get_my_list_returns_metadata_only(captured):
    """DigiKey returns an empty PartsList from GET /lists/{id} even though the schema
    declares it. get_my_list reflects that and tells callers to use get_parts_in_list."""
    captured["stubs"][("GET", "/mylists/v1/lists/list-abc")] = {
        "Id": "list-abc",
        "ListName": "Power supply BOM",
        "TotalParts": 4,
        "PartsList": [],  # always empty from this endpoint
    }
    out = get_my_list("list-abc")
    assert out["Id"] == "list-abc"
    assert out["TotalParts"] == 4
    # No PartsList key — callers must go to get_parts_in_list.
    assert "PartsList" not in out


def test_slim_list_part_extracts_selected_quantity():
    """Direct unit test of the part slimmer — covered through get_parts_in_list too,
    but worth pinning the quantity-extraction shape explicitly."""
    raw = {
        "UniqueId": "u-1",
        "RequestedPartNumber": "565-1571-1-ND",
        "DigiKeyPartNumber": "565-1571-1-ND",
        "ManufacturerPartNumber": "EEU-FR1V102",
        "Manufacturer": "Panasonic",
        "Description": "1000 µF 35V Alum Cap",
        "CustomerReference": "C1",
        "ReferenceDesignator": "C1, C2",
        "Notes": "5% tol",
        "QuantityAvailable": 12345,
        "PartStatus": "Active",
        "PartDetailUrl": "https://www.digikey.com/en/products/detail/...",
        "SelectedQuantityIndex": 0,
        "Quantities": [{"QuantityRequested": 10}],
        "Flags": {"NonStock": False},
        "Substitutes": [{"junk": True}],
    }
    slim = srv._slim_list_part(raw)
    assert slim["RequestedQuantity"] == 10
    assert slim["ReferenceDesignator"] == "C1, C2"
    assert "Flags" not in slim
    assert "Substitutes" not in slim


def test_get_parts_in_list_passes_pagination(captured):
    captured["stubs"][("GET", "/mylists/v1/lists/list-abc/parts")] = {
        "TotalParts": 42,
        "PartsList": [],
    }
    out = get_parts_in_list("list-abc", start_index=20, limit=10)
    assert out["TotalParts"] == 42
    method, url, data, params = captured["calls"][0]
    assert params == {"startIndex": 20, "limit": 10}


def test_update_my_list_name_url_encodes_path_segment(captured):
    """Names containing /, ?, # or % must be percent-encoded in the path,
    otherwise the URL would parse as a different route."""
    captured["stubs"][("PUT", "")] = None  # match-any; we inspect the URL below
    update_my_list_name("list-abc", "A/B?C#new name 100%")
    method, url, _, _ = captured["calls"][0]
    assert method == "PUT"
    # Each reserved character is percent-encoded; spaces become %20 (not '+').
    assert url.endswith("/listName/A%2FB%3FC%23new%20name%20100%25")


def test_validate_my_list_name_url_encodes_path_segment(captured):
    captured["stubs"][("GET", "")] = False
    validate_my_list_name("My/List?2026")
    _, url, _, _ = captured["calls"][0]
    assert url.endswith("/lists/validate/My%2FList%3F2026")


def test_make_user_request_url_encodes_query_params(monkeypatch):
    """urllib.parse.urlencode handles spaces, &, =, … — the prior f-string
    construction silently produced malformed URLs."""
    captured_url = {}

    class FakeResp:
        status_code = 200
        ok = True
        content = b"{}"
        def json(self): return {}

    def fake_get(url, headers=None, timeout=None):
        captured_url["url"] = url
        return FakeResp()

    monkeypatch.setattr(srv.requests, "get", fake_get)
    monkeypatch.setattr(srv, "_get_user_headers", lambda: {})

    srv._make_user_request(
        "GET", "https://example/x",
        params={"keyword": "a b&c", "limit": 10, "skip": None},
    )
    # None-valued param dropped; others encoded properly.
    assert captured_url["url"] == "https://example/x?keyword=a+b%26c&limit=10"


# ---------- request construction ----------

def test_add_parts_to_list_builds_requested_part_array(captured):
    captured["stubs"][("POST", "/mylists/v1/lists/list-abc/parts")] = ["new-u-1", "new-u-2"]
    out = add_parts_to_list(
        list_id="list-abc",
        parts=[
            {"part_number": "565-1571-1-ND", "quantity": 10, "reference_designator": "C1"},
            {"part_number": "1276-1010-1-ND", "quantity": 5, "notes": "feedback cap"},
        ],
    )
    assert out == ["new-u-1", "new-u-2"]
    method, url, data, params = captured["calls"][0]
    assert method == "POST"
    assert params == {"index": 0}
    assert len(data) == 2
    # First part: explicit reference designator.
    assert data[0] == {
        "RequestedPartNumber": "565-1571-1-ND",
        "Quantities": [{"Quantity": 10}],
        "SelectedQuantityIndex": 0,
        "ReferenceDesignator": "C1",
    }
    # Second part: notes set, no reference designator (omitted, not None-valued).
    assert data[1] == {
        "RequestedPartNumber": "1276-1010-1-ND",
        "Quantities": [{"Quantity": 5}],
        "SelectedQuantityIndex": 0,
        "Notes": "feedback cap",
    }


def test_add_parts_to_list_rejects_malformed_input(captured):
    with pytest.raises(ValueError, match="part_number"):
        add_parts_to_list(list_id="list-abc", parts=[{"part_number": "x"}])
    with pytest.raises(ValueError, match="non-empty"):
        add_parts_to_list(list_id="list-abc", parts=[])


def test_create_my_list_includes_only_provided_fields(captured):
    captured["stubs"][("POST", "/mylists/v1/lists")] = "new-list-id"
    create_my_list(list_name="My BOM", notes=None, tags=None)
    method, url, data, params = captured["calls"][0]
    # notes/tags omitted from body when None/empty so DigiKey doesn't store nulls.
    assert data == {"ListName": "My BOM"}

    captured["calls"].clear()
    create_my_list(list_name="Tagged BOM", tags=["rev-a"])
    _, _, data, _ = captured["calls"][0]
    assert data == {"ListName": "Tagged BOM", "Tags": ["rev-a"]}


def _ListPart_stub():
    """A realistic GET response shape — Quantities uses ListPartQuantity
    (QuantityRequested, PackOptions, …), which is what DigiKey actually returns."""
    return {
        "UniqueId": "u-1",
        "PartId": 999,
        "RequestedPartNumber": "565-1571-1-ND",
        "OriginalPartNumber": "565-1571-1-ND",
        "RequestedManufacturerName": "Panasonic",
        "CustomerReference": "old-cref",
        "ReferenceDesignator": "C1, C2",
        "Notes": "old note",
        "SelectedQuantityIndex": 0,
        "Quantities": [{
            "QuantityRequested": 10,
            "CalculatedQuantity": 10,
            "TargetPrice": 0.45,
            "SelectedPackType": "Cut Tape (CT)",
            "SelectedSubPackType": "",
            "PackOptions": [{"PackType": "Cut Tape (CT)"}, {"PackType": "Tape & Reel"}],
            "IsInactive": False,
        }],
    }


def test_update_part_in_list_with_quantity_change(captured):
    """quantity update: PUT body uses RequestedQuantity shape (Quantity, not
    QuantityRequested), preserves SelectedPackType/TargetPrice from the GET,
    and drops every other GET-shape field (PackOptions, CalculatedQuantity,
    IsInactive, …)."""
    captured["stubs"][("GET", "/mylists/v1/lists/list-abc/parts/u-1")] = _ListPart_stub()
    captured["stubs"][("PUT", "/mylists/v1/lists/list-abc/parts/u-1")] = None

    update_part_in_list(list_id="list-abc", unique_id="u-1", quantity=25, notes="new note")

    put_call = [c for c in captured["calls"] if c[0] == "PUT"][0]
    _, _, data, _ = put_call
    assert data["Notes"] == "new note"
    # Untouched fields preserved from the GET:
    assert data["CustomerReference"] == "old-cref"
    assert data["ReferenceDesignator"] == "C1, C2"
    assert data["RequestedPartNumber"] == "565-1571-1-ND"

    # One entry in PUT-shape; SelectedQuantityIndex collapsed to 0.
    assert data["SelectedQuantityIndex"] == 0
    assert data["Quantities"] == [{
        "Quantity": 25,
        "TargetPrice": 0.45,
        "SelectedPackType": "Cut Tape (CT)",
    }]
    # SelectedSubPackType was "" in the GET — falsy, so it's dropped (not echoed
    # back as an empty string, which would be uglier than just omitting the key).


def test_update_part_in_list_notes_only_preserves_quantity(captured):
    """The notes-only path (quantity=None) used to send the GET's Quantities array
    verbatim — which has QuantityRequested but no Quantity, so the PUT would zero
    out the stored quantity. Regression test for that bug."""
    captured["stubs"][("GET", "/mylists/v1/lists/list-abc/parts/u-1")] = _ListPart_stub()
    captured["stubs"][("PUT", "/mylists/v1/lists/list-abc/parts/u-1")] = None

    update_part_in_list(list_id="list-abc", unique_id="u-1", notes="just updating notes")

    put_call = [c for c in captured["calls"] if c[0] == "PUT"][0]
    _, _, data, _ = put_call
    assert data["Notes"] == "just updating notes"
    # Existing quantity (10) carried over under the PUT-shape key.
    assert data["Quantities"][0]["Quantity"] == 10


def test_update_part_in_list_with_bare_quantity_omits_packtype(captured):
    """When the GET has no pack-type info (a fresh part added without one), the
    PUT body shouldn't carry None-valued SelectedPackType/SelectedSubPackType
    keys — omit them entirely."""
    captured["stubs"][("GET", "/mylists/v1/lists/list-abc/parts/u-1")] = {
        "UniqueId": "u-1",
        "PartId": 999,
        "RequestedPartNumber": "565-1571-1-ND",
        "SelectedQuantityIndex": 0,
        "Quantities": [{"QuantityRequested": 5}],  # bare
    }
    captured["stubs"][("PUT", "/mylists/v1/lists/list-abc/parts/u-1")] = None

    update_part_in_list(list_id="list-abc", unique_id="u-1", quantity=7)

    put_call = [c for c in captured["calls"] if c[0] == "PUT"][0]
    _, _, data, _ = put_call
    assert data["Quantities"] == [{"Quantity": 7}]


# ---------- user-token bootstrap and refresh ----------

def test_bootstrap_from_seed_creates_cache(monkeypatch, tmp_path):
    """First call with no cache file but DIGIKEY_REFRESH_TOKEN_SEED set should
    refresh, write the cache, and rotate the refresh_token."""
    cache_path = tmp_path / "tokens.json"
    monkeypatch.setattr(srv, "DIGIKEY_TOKEN_CACHE", cache_path)
    monkeypatch.setattr(srv, "DIGIKEY_REFRESH_TOKEN_SEED", "seed-rt")
    monkeypatch.setattr(srv, "CLIENT_ID", "cid")
    monkeypatch.setattr(srv, "CLIENT_SECRET", "csecret")

    class FakeResp:
        status_code = 200
        text = "OK"
        ok = True
        def json(self):
            return {
                "access_token": "new-at",
                "refresh_token": "rotated-rt",  # DigiKey rotates on every refresh
                "expires_in": 1799,
            }

    posts = []
    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append((url, data))
        return FakeResp()
    monkeypatch.setattr(srv.requests, "post", fake_post)

    token = srv._get_user_access_token()
    assert token == "new-at"
    # The refresh request used the seed.
    assert posts[0][1]["refresh_token"] == "seed-rt"
    # Cache file was written with the *rotated* token, not the seed.
    cached = json.loads(cache_path.read_text())
    assert cached["refresh_token"] == "rotated-rt"
    assert cached["access_token"] == "new-at"
    assert cached["expires_at"] > time.time()


def test_returns_cached_token_when_not_near_expiry(monkeypatch, tmp_path):
    """A still-fresh access_token in the cache is returned without hitting DigiKey."""
    cache_path = tmp_path / "tokens.json"
    cache_path.write_text(json.dumps({
        "refresh_token": "rt",
        "access_token": "still-good",
        "expires_at": int(time.time()) + 3600,
    }))
    monkeypatch.setattr(srv, "DIGIKEY_TOKEN_CACHE", cache_path)

    posts = []
    def fake_post(*a, **kw):
        posts.append(a)
        raise AssertionError("should not have refreshed")
    monkeypatch.setattr(srv.requests, "post", fake_post)

    assert srv._get_user_access_token() == "still-good"
    assert posts == []


def test_no_seed_and_no_cache_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "DIGIKEY_TOKEN_CACHE", tmp_path / "missing.json")
    monkeypatch.setattr(srv, "DIGIKEY_REFRESH_TOKEN_SEED", None)
    with pytest.raises(RuntimeError, match="digikey-mcp-auth login"):
        srv._get_user_access_token()


def test_unwritable_cache_falls_back_to_memory(monkeypatch, tmp_path, caplog):
    """A read-only cache path doesn't crash; tokens stay in memory and a warning
    is logged once."""
    cache_path = tmp_path / "ro" / "tokens.json"
    cache_path.parent.mkdir()
    cache_path.parent.chmod(0o500)  # read+execute only — write will fail

    monkeypatch.setattr(srv, "DIGIKEY_TOKEN_CACHE", cache_path)
    monkeypatch.setattr(srv, "DIGIKEY_REFRESH_TOKEN_SEED", "seed-rt")
    monkeypatch.setattr(srv, "CLIENT_ID", "cid")
    monkeypatch.setattr(srv, "CLIENT_SECRET", "csecret")
    monkeypatch.setattr(srv, "_TOKEN_CACHE_WRITE_OK", True)  # reset module-global flag

    class FakeResp:
        status_code = 200
        ok = True
        def json(self):
            return {"access_token": "at", "refresh_token": "rt2", "expires_in": 1799}
    monkeypatch.setattr(srv.requests, "post", lambda *a, **kw: FakeResp())

    with caplog.at_level("WARNING"):
        token = srv._get_user_access_token()
    assert token == "at"
    assert any("Could not persist token cache" in r.message for r in caplog.records)

    # Restore perms so pytest can clean up the tmp_path.
    cache_path.parent.chmod(0o700)
