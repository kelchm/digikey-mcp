"""Offline tests for the parametric search code path.

These run against captured API snapshots in tests/fixtures/; see tests/conftest.py
and tests/refresh_snapshots.py for the snapshot mechanism.
"""
import pytest

import digikey_mcp_server as srv

find_components = srv.find_components.fn
keyword_search = srv.keyword_search.fn

CATEGORY_ID = "58"  # Aluminum Electrolytic Capacitors


def test_category_name_lookup():
    assert srv._get_category_name(CATEGORY_ID) == "Aluminum Electrolytic Capacitors"


def test_internal_get_parametric_filters_returns_full_histogram():
    """The internal helper still returns the full data for find_components to consume."""
    filters = srv._get_parametric_filters(CATEGORY_ID)
    by_name = {p["ParameterName"]: p for p in filters}
    cap = by_name["Capacitance"]
    assert len(cap["FilterValues"]) > 100
    value_names = [v["ValueName"] for v in cap["FilterValues"]]
    assert "0.1 µF" in value_names
    assert "470 µF" in value_names


def test_public_get_parametric_filters_returns_summary_by_default():
    """The public tool returns a small summary, not the megabyte-scale full histogram."""
    summary = srv.get_parametric_filters.fn(CATEGORY_ID)
    assert isinstance(summary, list)
    by_name = {p["ParameterName"]: p for p in summary}
    cap = by_name["Capacitance"]
    assert cap["TotalCount"] > 100
    assert len(cap["SampleValues"]) == 3
    # No FilterValues key — that's the whole point of summary mode.
    assert "FilterValues" not in cap


def test_public_get_parametric_filters_with_parameter_name_capped():
    """Default max_values=100 truncates Capacitance (685 values)."""
    result = srv.get_parametric_filters.fn(CATEGORY_ID, parameter_name="Capacitance")
    assert result["ParameterName"] == "Capacitance"
    assert result["Truncated"] is True
    assert len(result["FilterValues"]) == 100
    assert result["TotalCount"] == 685


def test_public_get_parametric_filters_unlimited():
    result = srv.get_parametric_filters.fn(CATEGORY_ID, parameter_name="Capacitance", max_values=0)
    assert result["Truncated"] is False
    assert len(result["FilterValues"]) == result["TotalCount"]


def test_discrete_value_match():
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": "470 µF"},
        limit=3,
    )
    assert result["ProductsCount"] > 0
    assert result["AppliedFilters"] == {"Capacitance": ["470 µF"]}
    assert len(result["Products"]) > 0
    for p in result["Products"]:
        assert p["ParameterMap"]["Capacitance"] == "470 µF"


def test_same_unit_range():
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": {"min": "100 µF", "max": "470 µF"}},
        limit=3,
    )
    # Range matched 45 values, so AppliedFilters is summarized rather than a full list.
    summary = result["AppliedFilters"]["Capacitance"]
    assert summary["MatchedCount"] == 45
    assert summary["From"] == "100 µF"
    assert summary["To"] == "470 µF"
    # Verify the actual products returned have a Capacitance value (within the bounds — the
    # bound enforcement is exercised in test_cross_unit_range and the unit tests).
    for p in result["Products"]:
        assert p["ParameterMap"].get("Capacitance") is not None


def test_match_values_expands_to_magnitude_aliases():
    """When DigiKey enumerates the same physical value under multiple unit strings
    (e.g. '1 mF' and '1000 µF' are separate buckets that may select different product
    subsets), we must pass ALL magnitude-equivalent ValueIds to DigiKey — not just the
    one the user literally typed. Otherwise we silently drop products tagged only
    under the other alias."""
    param = {
        "ParameterName": "Capacitance",
        "ParameterType": "UnitOfMeasure",
        "FilterValues": [
            {"ValueId": "1 mF",    "ValueName": "1 mF",    "ProductCount": 1000},
            {"ValueId": "1000 µF", "ValueName": "1000 µF", "ProductCount": 800},
            {"ValueId": "470 µF",  "ValueName": "470 µF",  "ProductCount": 500},
        ],
    }
    # Exact-match input → expands to both aliases.
    assert sorted(srv._match_values(param, "1 mF")) == sorted(["1 mF", "1000 µF"])
    assert sorted(srv._match_values(param, "1000 µF")) == sorted(["1 mF", "1000 µF"])
    # Cross-prefix input not in the histogram → resolves via fallback to all aliases.
    assert sorted(srv._match_values(param, "0.001 F")) == sorted(["1 mF", "1000 µF"])
    # Unambiguous single value → no alias expansion needed.
    assert srv._match_values(param, "470 µF") == ["470 µF"]


def test_match_values_falls_back_via_cross_prefix_quantity():
    """`0.47 mF` should resolve to `470 µF` even though the literal string isn't in the
    histogram — closes the asymmetry between the discrete and range matching paths."""
    param = {
        "ParameterName": "Capacitance",
        "ParameterType": "UnitOfMeasure",
        "FilterValues": [
            {"ValueId": "470 µF", "ValueName": "470 µF", "ProductCount": 500},
            {"ValueId": "100 µF", "ValueName": "100 µF", "ProductCount": 300},
        ],
    }
    assert srv._match_values(param, "0.47 mF") == ["470 µF"]
    assert srv._match_values(param, "0.0001 F") == ["100 µF"]


def test_range_on_coupled_unit_parameter_is_rejected():
    """Coupled-unit parameters (e.g. 'Ripple Current @ Low Frequency' valued '500 mA @ 100 kHz')
    have two axes per value. A range query is semantically ill-defined; under the old behavior
    pint silently computed scalar products and returned plausible-looking garbage. Now it errors."""
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Ripple Current @ Low Frequency": {"min": "500 mA @ 100 kHz", "max": "1 A @ 100 kHz"}},
        )
    msg = str(exc.value)
    assert "Ripple Current" in msg
    assert "coupled-unit" in msg.lower()
    # The error must NOT mention specific values from the input — that would imply the bounds
    # were considered, which they explicitly aren't.
    assert "500 mA" not in msg
    assert "1 A" not in msg


def test_cross_unit_range():
    """The headline pint capability: bound in mF, histogram in µF. Also verifies the
    magnitude-dedup behavior — cat 58's Capacitance histogram has many mF/µF aliases
    in this range, so MatchedCount must reflect distinct physical values, not raw
    bucket count. Before dedup this range returned 212 buckets; ~104 are aliases."""
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": {"min": "0.5 mF", "max": "5 mF"}},
        limit=2,
    )
    summary = result["AppliedFilters"]["Capacitance"]
    # 0.5 mF == 500 µF, 5 mF == 5000 µF; the lower bound is 500 µF.
    assert summary["From"] == "500 µF"
    # 470 µF must not be in the sample (it's below the lower bound).
    assert "470 µF" not in summary["Sample"]
    # Sample must not contain two entries with the same base-unit magnitude.
    sample_quantities = [srv._to_quantity(s) for s in summary["Sample"]]
    base_magnitudes = [q.to_base_units().magnitude for q in sample_quantities if q is not None]
    assert len(base_magnitudes) == len(set(base_magnitudes)), "Sample contains alias dupes"


def test_keyword_search_returns_raw_digikey_shape_with_category_filter():
    """keyword_search is a thin wrapper around POST /search/keyword. Verify that:
    - the response keeps DigiKey's raw shape (PascalCase, full Product objects), and
    - CategoryFilter is honored (every product belongs to the requested leaf category).
    The second check guards against regressions in the FilterOptionsRequest nesting that
    would silently broaden the search."""
    result = keyword_search(keywords="Nichicon", category_id="58", limit=3)
    assert "ProductsCount" in result
    assert "Products" in result
    assert isinstance(result["Products"], list) and len(result["Products"]) > 0
    for p in result["Products"]:
        # Raw DigiKey shape — not the slim find_components shape.
        assert "ManufacturerProductNumber" in p
        assert "ProductVariations" in p
        # CategoryFilter should land us in cat 58 (Aluminum Electrolytic Capacitors).
        leaf_cats = [c.get("Name") for c in (p.get("Category") or {}).get("ChildCategories") or []]
        assert "Aluminum Electrolytic Capacitors" in leaf_cats


PARENT_CATEGORY_ID = "20"  # Connectors, Interconnects — has no parametric filters


def test_parent_category_with_attributes_raises_helpful_error():
    """Calling find_components with attributes against a parent category should fail
    with a message that points at the category, not at the attribute name."""
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=PARENT_CATEGORY_ID,
            attributes={"Pin Count": "4"},
        )
    msg = str(exc.value)
    assert PARENT_CATEGORY_ID in msg
    assert "Connectors" in msg
    assert "leaf" in msg.lower()
    # Make sure the error is NOT the generic "attribute not found" path.
    assert "Pin Count" not in msg


def test_parent_category_with_no_attributes_still_works():
    """Without attribute filters, find_components on a parent category should just
    return whatever DigiKey gives — no early error."""
    result = find_components(category_id=PARENT_CATEGORY_ID, limit=3)
    assert "ProductsCount" in result
    assert "Products" in result


def test_bad_attribute_name_suggests_close_candidate():
    """Typo in attribute name should produce a 'did you mean' with the most likely
    intended name, not a dump of all 18 attributes."""
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Capacitancce": "470 µF"},
            limit=1,
        )
    msg = str(exc.value)
    assert "Capacitancce" in msg
    assert "Did you mean" in msg
    assert "Capacitance" in msg  # specifically suggested as a close match


def test_bad_value_suggests_numerically_nearest():
    """Typo in a unit-bearing value should produce a 'did you mean' ranked by
    magnitude proximity (not alphabetical order). Passing '473 µF' should suggest
    '470 µF' near the top."""
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Capacitance": "473 µF"},
            limit=1,
        )
    msg = str(exc.value)
    assert "473 µF" in msg
    assert "Did you mean" in msg
    assert "470 µF" in msg  # the obvious magnitude-nearest histogram bucket


def test_bad_value_non_quantity_input_falls_back_to_string_similarity():
    """Garbage input (not a parseable quantity) shouldn't crash — falls back to
    edit-distance suggestion against value names."""
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Capacitance": "12345 zorks"},
            limit=1,
        )
    msg = str(exc.value)
    assert "12345 zorks" in msg
    assert "Did you mean" in msg


def test_to_quantity_handles_common_formats():
    assert srv._to_quantity("470 µF") is not None
    assert srv._to_quantity("12.5mm") is not None
    assert srv._to_quantity("100 mF") is not None
    assert srv._to_quantity("1 kΩ") is not None
    assert srv._to_quantity("±20%") is not None
    assert srv._to_quantity(None) is None
    assert srv._to_quantity("") is None


@pytest.mark.parametrize("bad_input", [
    "8000 Hrs @ 105°C",      # UndefinedUnitError — 'Hrs' not in registry
    "TO-220-3",              # UndefinedUnitError — 'TO' not in registry
    "Approx. 470 µF",        # UndefinedUnitError — 'Approx' not in registry
    "   ",                   # AssertionError on whitespace
    "",                      # ValueError on empty
    "{'foo': 'bar'}",        # AssertionError on garbage (str() of a dict, in case anyone passes one)
])
def test_to_quantity_returns_none_on_known_bad_inputs(bad_input):
    """Every exception pint raises across the DigiKey filter-value corpus must be caught
    and returned as None — that's the contract the range-matching code depends on."""
    assert srv._to_quantity(bad_input) is None


@pytest.mark.parametrize("compound_input,reason", [
    ("500 mA @ 100 kHz",  "pint reads '@' as scalar product → 50,000 mA·kHz (compound dim)"),
    ("1 A @ 100 kHz",     "same — would compare against other coupled values as products"),
    ("100/120/200V",      "pint reads '/' as division → 100/120/200 V = ~0.004 V"),
    ("1.5 mA, 3 mA",      "comma — parser behavior is dialect-dependent"),
])
def test_to_quantity_rejects_compound_expressions(compound_input, reason):
    """Strings with operators pint would silently evaluate as math must return None up front
    so range matching can't produce wrong-magnitude results (see the Ripple Current bug)."""
    assert srv._to_quantity(compound_input) is None, reason


def test_to_quantity_propagates_unrelated_errors(monkeypatch):
    """A genuine bug (typo, ImportError, etc.) must NOT be silently caught — the whole
    point of narrowing the except clause was to surface those during development."""
    def broken_quantity(_s):
        raise AttributeError("simulated typo in pint API")
    monkeypatch.setattr(srv._UREG, "Quantity", broken_quantity)
    with pytest.raises(AttributeError, match="simulated typo"):
        srv._to_quantity("470 µF")


def test_to_quantity_cross_unit_equality():
    assert srv._to_quantity("0.5 mF") == srv._to_quantity("500 µF")
    assert srv._to_quantity("0.5 mF") < srv._to_quantity("1 F")
