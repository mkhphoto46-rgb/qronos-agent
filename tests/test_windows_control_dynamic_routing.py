from unittest.mock import patch

from core.runtime_bridge import _windows_control_route_promotion


def test_installed_dynamic_app_command_is_promoted_to_computer():
    with patch(
        "core.runtime_bridge.resolve_application_id",
        return_value="discovered:exe:photoshop",
    ):
        assert _windows_control_route_promotion(
            "ادوبی فتوشاپ رو باز کن"
        ) is True


def test_unknown_open_request_is_not_promoted():
    with patch(
        "core.runtime_bridge.resolve_application_id",
        side_effect=LookupError("not installed"),
    ):
        assert _windows_control_route_promotion(
            "کتاب داستان را باز کن"
        ) is False
