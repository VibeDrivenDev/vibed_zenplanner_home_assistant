"""Package boundaries the UI modules are not allowed to cross."""

import ast
import json
from pathlib import Path
from typing import get_args

from custom_components.zenplanner.models import AuthFailure

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "zenplanner"
UI_FILES = ("calendar.py", "sensor.py", "config_flow.py", "__init__.py")
FORBIDDEN = (
    "_Api",
    "plan_reserve",
    "plan_cancel",
    "fold_agenda",
    "payloadArray",
    "appointmentId",
    "attendanceId",
    "X-Auth-Refresh",
    "SUCCESS_MULTIPLE_ORGS",
    "ignoreLimit",
    "memberapp",
)


def _keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _auth_failures() -> tuple[str, ...]:
    alias = AuthFailure
    if hasattr(alias, "__value__"):
        alias = alias.__value__
    values = get_args(alias)
    assert values
    return values


def test_strings_cover_auth_failures_and_service_fields() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text())
    keys = set(_keys(strings))
    for name in _auth_failures():
        assert name in keys
    for name in ("cannot_connect", "entity_id", "class_id", "join_waitlist"):
        assert name in keys


def test_ui_modules_do_not_name_wire_or_planners() -> None:
    for name in UI_FILES:
        text = (PACKAGE / name).read_text()
        for token in FORBIDDEN:
            assert token not in text, f"{name} contains {token}"
    calendar = (PACKAGE / "calendar.py").read_text()
    assert "async_create_event" not in calendar
    assert "async_delete_event" not in calendar
    assert "supported_features" in calendar
    const = (PACKAGE / "const.py").read_text()
    assert "zenplanner.com" not in const
    assert "8D052224" not in const


def test_models_do_not_import_home_assistant() -> None:
    tree = ast.parse((PACKAGE / "models.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert not module.startswith("homeassistant")
            assert "aiohttp" not in module
