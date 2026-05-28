"""Offline tests for the parametric search code path.

These run against captured API snapshots in tests/fixtures/; see tests/conftest.py
and tests/refresh_snapshots.py for the snapshot mechanism.
"""
import pytest

import digikey_mcp_server as srv

find_components = srv.find_components.fn

CATEGORY_ID = "58"  # Aluminum Electrolytic Capacitors


def test_category_name_lookup():
    assert srv._get_category_name(CATEGORY_ID) == "Aluminum Electrolytic Capacitors"


def test_parametric_filters_returns_full_histogram():
    filters = srv._get_parametric_filters(CATEGORY_ID)
    by_name = {p["ParameterName"]: p for p in filters}
    cap = by_name["Capacitance"]
    # The full per-category histogram (vs. the 1-3 buckets we'd get without the
    # category-name-as-keyword trick) is the central thing this MCP unlocks.
    assert len(cap["FilterValues"]) > 100
    value_names = [v["ValueName"] for v in cap["FilterValues"]]
    assert "0.1 µF" in value_names
    assert "470 µF" in value_names


def test_discrete_value_match():
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": "470 µF"},
        limit=3,
    )
    assert result["products_count"] > 0
    assert result["applied_filters"] == {"Capacitance": ["470 µF"]}
    assert len(result["products"]) > 0
    for p in result["products"]:
        assert p["parameters"]["Capacitance"] == "470 µF"


def test_same_unit_range():
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": {"min": "100 µF", "max": "470 µF"}},
        limit=3,
    )
    matched = result["applied_filters"]["Capacitance"]
    assert "100 µF" in matched
    assert "470 µF" in matched
    assert "0.1 µF" not in matched  # below the bound
    assert "1000 µF" not in matched  # above the bound


def test_cross_unit_range():
    """The headline pint capability: bound in mF, histogram in µF."""
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": {"min": "0.5 mF", "max": "5 mF"}},
        limit=2,
    )
    matched = result["applied_filters"]["Capacitance"]
    # 0.5 mF == 500 µF and 5 mF == 5000 µF; the histogram values in between are matched.
    assert "500 µF" in matched
    assert "1000 µF" in matched
    assert "470 µF" not in matched  # 470 µF < 500 µF lower bound


def test_client_side_sort_by_lifetime_desc():
    """`Lifetime @ Temp.` isn't a pure unit string; sort falls back to the leading number."""
    result = find_components(
        category_id=CATEGORY_ID,
        attributes={"Capacitance": {"min": "220 µF"}},
        sort_by_attribute="Lifetime @ Temp.",
        sort_order="Descending",
        limit=5,
    )
    lifetimes = [p["parameters"].get("Lifetime @ Temp.") for p in result["products"]]
    # Pull the leading integer from each; the sort should be non-increasing.
    import re
    numbers = [int(re.search(r"\d+", lt).group(0)) for lt in lifetimes if lt]
    assert numbers == sorted(numbers, reverse=True)


def test_bad_attribute_name_lists_candidates():
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Capacitancce": "470 µF"},
            limit=1,
        )
    msg = str(exc.value)
    assert "Capacitancce" in msg
    assert "Capacitance" in msg  # listed as a candidate


def test_bad_value_for_known_attribute_lists_samples():
    with pytest.raises(ValueError) as exc:
        find_components(
            category_id=CATEGORY_ID,
            attributes={"Capacitance": "12345 zorks"},
            limit=1,
        )
    assert "12345 zorks" in str(exc.value)


def test_to_quantity_handles_common_formats():
    assert srv._to_quantity("470 µF") is not None
    assert srv._to_quantity("12.5mm") is not None
    assert srv._to_quantity("100 mF") is not None
    assert srv._to_quantity("1 kΩ") is not None
    assert srv._to_quantity("±20%") is not None
    assert srv._to_quantity(None) is None
    assert srv._to_quantity("") is None
    # DigiKey strings pint can't parse — must return None so range matching falls back.
    assert srv._to_quantity("8000 Hrs @ 105°C") is None


def test_to_quantity_cross_unit_equality():
    assert srv._to_quantity("0.5 mF") == srv._to_quantity("500 µF")
    assert srv._to_quantity("0.5 mF") < srv._to_quantity("1 F")
