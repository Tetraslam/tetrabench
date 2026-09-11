"""Version-matched ModelInfo alias selection, independent of display labels."""

import pytest

from tetrabench.native_control import ControlError, select_model_info


def rows():
    return [
        {
            "value": "default",
            "resolvedModel": "claude-opus-5",
            "displayName": "Default",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "high", "max"],
            "supportsAdaptiveThinking": True,
        },
        {
            "value": "claude-opus-5[1m]",
            "resolvedModel": "claude-opus-5",
            "displayName": "Opus 1M",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "high", "max"],
            "supportsAdaptiveThinking": True,
        },
    ]


def test_requested_native_selector_wins_over_equal_resolved_alias():
    values = rows()
    assert select_model_info(values, "claude-opus-5[1m]", "claude-opus-5") is values[1]
    assert select_model_info(values, "default", "claude-opus-5") is values[0]


def test_equal_descriptors_allow_a_resolved_id_fallback():
    assert (
        select_model_info(rows(), "claude-opus-5", "claude-opus-5")["resolvedModel"]
        == "claude-opus-5"
    )


def test_same_selector_conflicting_descriptor_is_refused():
    values = rows()
    values.append({**values[0], "supportedEffortLevels": ["low"]})
    with pytest.raises(ControlError, match="conflicting"):
        select_model_info(values, "default", "claude-opus-5")


def test_conflicting_resolved_fallback_is_refused_without_guessing_alias():
    values = rows()
    values[1]["supportedEffortLevels"] = ["low"]
    with pytest.raises(ControlError, match="conflicting"):
        select_model_info(values, "claude-opus-5", "claude-opus-5")
