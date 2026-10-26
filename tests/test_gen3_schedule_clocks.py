"""Keep Gen3 weekly schedule times correct across Vacuula clock versions."""

# ruff: noqa: F811

from copy import deepcopy

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_service_regressions import _call, action_setup  # noqa: F401


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0-b2", "1.1.0-b3", "1.1.0", "1.2.0"])
@pytest.mark.parametrize("now", ["2026-01-19T09:00:00Z", "2026-10-19T09:00:00Z"])
async def test_local_slots_follow_the_server_clock(
    hass, action_setup, freezer, version, now
):  # noqa: F811
    """Keep matching local times on new servers and legacy UTC times on old ones."""
    freezer.move_to(now)
    hass.config.time_zone = "Asia/Jerusalem"
    coordinator = action_setup[0]
    coordinator.server_info = {"version": version, "timezone": "Asia/Jerusalem"}
    event = {"mode": 1, "day": 1, "startTime": "09:00"}
    await _call(hass, action_setup, "set_schedule", events=[event])
    expected = "09:00"
    if version in {"1.0.0", "1.1.0-b2"}:
        expected = "07:00" if now.startswith("2026-01") else "06:00"
    assert coordinator.command_payloads[-1]["params"]["events"] == [
        {**event, "startTime": expected}
    ]
    assert event["startTime"] == "09:00"


@pytest.mark.parametrize(
    (
        "input_zone",
        "robot_zone",
        "basis",
        "day",
        "time",
        "expected_day",
        "expected_time",
    ),
    [
        ("Asia/Jerusalem", "Asia/Jerusalem", "utc", 6, "23:30", 0, "01:30"),
        ("Asia/Jerusalem", "America/New_York", "local", 0, "00:30", 6, "17:30"),
        ("UTC", "Asia/Kathmandu", "local", 6, "23:30", 0, "05:15"),
        ("Asia/Jerusalem", "America/New_York", "utc", 0, "00:30", 6, "19:30"),
    ],
)
async def test_explicit_timezones_preserve_weekly_instants(  # noqa: F811
    hass,
    action_setup,
    freezer,
    input_zone,
    robot_zone,
    basis,
    day,
    time,
    expected_day,
    expected_time,
):
    """Convert weekly slots across UTC, local zones and fractional offsets."""
    freezer.move_to("2026-01-19T09:00:00Z")
    hass.config.time_zone = input_zone
    coordinator = action_setup[0]
    coordinator.server_info = {"version": "1.1.0", "timezone": robot_zone}
    await _call(
        hass,
        action_setup,
        "set_schedule",
        time_basis=basis,
        events=[{"mode": 2, "day": day, "startTime": time}],
    )
    assert coordinator.command_payloads[-1]["params"]["events"] == [
        {"mode": 2, "day": expected_day, "startTime": expected_time}
    ]


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
async def test_robot_basis_round_trips_raw_schedule_events(hass, action_setup, version):  # noqa: F811
    """Preserve raw times and identified zones without mutating a read response."""
    coordinator = action_setup[0]
    coordinator.server_info = {"version": version, "timezone": "Asia/Jerusalem"}
    event = {
        "mode": 1,
        "day": 6,
        "startTime": "23:30",
        "boundary": {"id": "zone1", "name": "Living room"},
    }
    original = deepcopy(event)
    coordinator.async_command.return_value = {"data": {"events": [event]}}
    response = await _call(hass, action_setup, "get_data", resource="schedule")
    await _call(
        hass,
        action_setup,
        "set_schedule",
        events=response["data"]["events"],
        time_basis="robot",
    )
    assert coordinator.command_payloads[-1]["params"]["events"] == [
        {"mode": 1, "day": 6, "startTime": "23:30", "boundaryId": "zone1"}
    ]
    assert event == original


@pytest.mark.parametrize(
    "server_info",
    [
        {},
        {"version": None},
        {"version": "unknown"},
        {"version": "1.1.0"},
        {"version": "1.1.0", "timezone": "Invalid/Timezone"},
        {"version": "1.1.0", "timezone": 120},
    ],
)
@pytest.mark.parametrize("basis", ["local", "utc"])
async def test_unknown_clock_metadata_rejects_converted_writes(  # noqa: F811
    hass, action_setup, server_info, basis
):
    """Refuse to replace schedules with times in an unverified target clock."""
    coordinator = action_setup[0]
    coordinator.server_info = server_info
    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            action_setup,
            "set_schedule",
            time_basis=basis,
            events=[{"mode": 1, "day": 1, "startTime": "09:00"}],
        )
    assert error.value.translation_key == "schedule_clock_unknown"
    assert coordinator.command_payloads == []


@pytest.mark.parametrize("clear", [False, True])
async def test_raw_slots_and_clearing_need_no_clock_metadata(hass, action_setup, clear):  # noqa: F811
    """Permit explicit raw slots and empty replacements when metadata is missing."""
    coordinator = action_setup[0]
    coordinator.server_info = {}
    events = [] if clear else [{"mode": 1, "day": 1, "startTime": "09:00"}]
    await _call(
        hass,
        action_setup,
        "set_schedule",
        events=events,
        time_basis="local" if clear else "robot",
    )
    assert coordinator.command_payloads[-1]["params"]["events"] == events
