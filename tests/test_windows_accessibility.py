from core.windows_accessibility import _parse


def test_accessibility_parser_reads_edit_control():
    item = _parse(
        """
        {
          "ok": true,
          "name": "Address and search bar",
          "automation_id": "addressEditBox",
          "class_name": "Edit",
          "control_type": "ControlType.Edit",
          "native_hwnd": 42,
          "ancestor_hwnds": [42, 100],
          "has_keyboard_focus": true,
          "is_keyboard_focusable": true,
          "is_enabled": true,
          "left": 100,
          "top": 50,
          "width": 600,
          "height": 32
        }
        """
    )

    assert item.ok
    assert item.likely_text_input
    assert item.belongs_to_hwnd(100)


def test_non_edit_button_is_not_text_input():
    item = _parse(
        """
        {
          "ok": true,
          "name": "Search",
          "automation_id": "searchButton",
          "class_name": "Button",
          "control_type": "ControlType.Button",
          "native_hwnd": 42,
          "ancestor_hwnds": [42, 100],
          "has_keyboard_focus": true,
          "is_keyboard_focusable": true,
          "is_enabled": true,
          "left": 100,
          "top": 50,
          "width": 40,
          "height": 40
        }
        """
    )

    assert item.ok
    assert not item.likely_text_input


def test_failed_lookup_is_preserved():
    item = _parse(
        """
        {
          "ok": false,
          "reason": "no focused element"
        }
        """
    )

    assert not item.ok
    assert "focused" in item.reason



def test_uia_candidate_recognises_edit_role():
    from core.windows_accessibility import UiElementCandidate

    item = UiElementCandidate(
        name="Address",
        automation_id="address",
        class_name="Edit",
        control_type="ControlType.Edit",
        native_hwnd=1,
        has_keyboard_focus=False,
        is_keyboard_focusable=True,
        is_enabled=True,
        left=100,
        top=100,
        width=500,
        height=40,
    )

    assert item.usable
    assert item.likely_text_input
    assert item.contains(200, 120)
    assert not item.contains(50, 50)


def test_menu_item_is_not_a_text_input_candidate():
    from core.windows_accessibility import UiElementCandidate

    item = UiElementCandidate(
        name="Extra large icons",
        automation_id="",
        class_name="ToggleMenuFlyoutItem",
        control_type="ControlType.MenuItem",
        native_hwnd=1,
        has_keyboard_focus=True,
        is_keyboard_focusable=True,
        is_enabled=True,
        left=100,
        top=100,
        width=200,
        height=40,
    )

    assert item.usable
    assert not item.likely_text_input
