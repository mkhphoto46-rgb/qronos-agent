from unittest.mock import patch

import pytest

from core.web_providers import requests_transport
from core.computer_executor import (
    _default_gui_action, KNOWN_APPS, ComputerOperation,
)


def test_legacy_web_transport_refuses_before_network_io():
    with patch("requests.request") as request:
        with pytest.raises(PermissionError, match="browser adapter"):
            requests_transport("GET", "https://example.com")
        request.assert_not_called()


def test_ungrounded_paint_input_is_disabled():
    with pytest.raises(RuntimeError, match="grounding"):
        _default_gui_action(KNOWN_APPS["paint"], ComputerOperation.DRAW_BASIC_HOUSE)
