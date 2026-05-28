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


def test_cross_unit_range():
    """The headline pint capability: bound in mF, histogram in µF."""
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
