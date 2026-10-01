"""Offline checks for the temporary capture build (fork main only)."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox import diagnostics  # noqa: E402
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    GatewaySnapshot,
    VersionInfo,
)
from custom_components.zehnder_connectbox.profiles import (  # noqa: E402
    CAPTURE_PROPERTY_GROUPS,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import (  # noqa: E402
    OperationType,
    decode_rooms,
    decode_run_state,
)
from custom_components.zehnder_connectbox.session import (  # noqa: E402
    GatewayResponseError,
)
from homeassistant.helpers.json import json_bytes  # noqa: E402

SERIAL = "SN98765"
ROOM_NAME = "Wohnzimmer"


def prop(key, value: bytes) -> bytes:
    key_message = b"".join(
        encode_uint(number, part)
        for number, part in zip((1, 2, 3, 4, 5, 6), (30, 255, 0, *key), strict=True)
    )
    return encode_bytes(101, encode_bytes(1, key_message) + encode_bytes(2, value))


def rooms_message(properties=()) -> bytes:
    device = b"".join(
        [
            encode_uint(1, 7),
            encode_string(2, SERIAL),
            encode_uint(5, 30),
            encode_uint(7, 67),
            encode_bytes(8, encode_uint(1, 2) + encode_uint(2, 51)),
            encode_uint(10, 0),
            encode_uint(20, 1),
            *(prop(key, value) for key, value in properties),
            encode_uint(110, 1),
        ]
    )
    room = b"".join(
        [
            encode_uint(1, 1),
            encode_string(2, ROOM_NAME),
            encode_bytes(8, device),
            encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 5)),
            encode_bytes(19, encode_uint(1, 2) + encode_uint(2, 0)),
            encode_uint(21, 4),
            encode_uint(22, 15),
            encode_bytes(23, b"Some text"),
            encode_uint(61, 1790881200),
        ]
    )
    return encode_bytes(1, room)


def run_state_message(summer: int) -> bytes:
    inner = b"".join(
        [
            encode_uint(1, 2),
            encode_uint(2, 2),
            encode_uint(7, 0),
            encode_uint(12, summer),
            encode_bytes(13, encode_uint(1, 3) + encode_uint(2, 1)),
            encode_bytes(
                4,
                encode_uint(1, 5)
                + encode_bytes(2, encode_uint(1, 7) + encode_uint(3, 1790881200))
                + encode_bytes(3, b"Sommer"),
            ),
        ]
    )
    return encode_bytes(1, inner)


def make_client(reject_class=None, answer_keys=None):
    client = client_mod.ConnectBoxClient("192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32)
    requested: list[tuple[int, int, int, int]] = []

    def request(request, confirm, body=b"", **_kw):
        assert request is OperationType.DEVICE_PROPERTIES_REQUEST
        fields = decode_fields(body)
        key = decode_fields(next(f.value for f in fields if f.number == 3))
        identity = tuple(uint_value(key, n) for n in (4, 5, 6))
        requested.append((uint_value(fields, 1), *identity))
        if identity[0] == reject_class:
            raise GatewayResponseError(2, None)
        return SimpleNamespace(body=b"")

    session = SimpleNamespace(request=request)
    client._connected_session = lambda: session
    keys = answer_keys if answer_keys is not None else [
        key for group in CAPTURE_PROPERTY_GROUPS for key in group
    ]
    properties = [(key, bytes([key[2]])) for key in keys]
    properties.append(((38, 0, 99), b"long text value"))
    client._read_rooms = lambda: decode_rooms(rooms_message(properties))
    client._read_run_state = lambda: decode_run_state(run_state_message(1))
    return client, requested


def test_capture_requests_groups_and_collects_values():
    client, requested = make_client()
    run_state, rooms, result = client.capture(
        ((7, 30, CAPTURE_PROPERTY_GROUPS),), deadline=time.monotonic() + 30
    )
    assert run_state.summer_ventilation is True
    assert rooms[0].room_id == 1
    # START ... FINISH per group; 2-element groups are START + FINISH.
    assert requested[0] == (0, 38, 0, 5)
    assert requested[1] == (1, 38, 0, 6)
    assert requested[2] == (2, 38, 0, 8)
    assert requested[3] == (0, 38, 0, 9)
    assert requested[6] == (0, 38, 0, 13)
    assert requested[7] == (2, 38, 0, 14)
    assert len(requested) == sum(len(group) for group in CAPTURE_PROPERTY_GROUPS)
    values = result[7]["values"]
    assert values["38.0.11"] == {"hex": "0b", "uint_le": 11}
    assert result[7]["missing"] == []
    assert result[7]["errors"] == []


def test_capture_isolates_rejected_groups():
    keys = [key for group in CAPTURE_PROPERTY_GROUPS for key in group if key[0] == 38]
    client, requested = make_client(reject_class=36, answer_keys=keys)
    client_mod.CAPTURE_SETTLE_TIMEOUT = 0.6
    _run_state, _rooms, result = client.capture(
        ((7, 30, CAPTURE_PROPERTY_GROUPS),), deadline=time.monotonic() + 30
    )
    assert "38.0.19" in result[7]["values"]
    assert result[7]["missing"] == ["36.0.3", "36.0.4", "36.1.3", "36.1.4"]
    assert len(result[7]["errors"]) == 2
    assert result[7]["errors"][0].startswith("36.0.3,36.0.4: GatewayResponseError")


def test_capture_without_targets_reads_state():
    client, requested = make_client()
    run_state, rooms, result = client.capture((), deadline=time.monotonic() + 5)
    assert requested == []
    assert result == {}
    assert rooms and run_state.run_mode == 2


class FakeCoordinator:
    def __init__(self, data, capture):
        self.data = data
        self.last_update_success = True
        self._capture = capture

    async def async_capture(self):
        if isinstance(self._capture, Exception):
            raise self._capture
        return self._capture


def snapshot(summer=0):
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None),
        decode_run_state(run_state_message(summer)),
        decode_rooms(rooms_message([((37, 0, 1), b"\x85")])),
    )


def diagnostics_for(capture):
    entry = SimpleNamespace(runtime_data=FakeCoordinator(snapshot(), capture), version=1)
    return asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))


def test_diagnostics_capture_fresh_read():
    fresh_run_state = decode_run_state(run_state_message(1))
    fresh_rooms = decode_rooms(rooms_message([((38, 0, 11), b"\x02")]))
    result = diagnostics_for(
        (fresh_run_state, fresh_rooms, {7: {"values": {}, "missing": [], "errors": []}})
    )
    capture = result["capture"]
    assert capture["source"] == "fresh read"
    assert capture["app_properties"] == {"7": {"values": {}, "missing": [], "errors": []}}
    fields = capture["fields"]
    assert fields["run_state.f12"] == 1
    assert fields["run_state.f13.f1"] == 3
    assert fields["run_state.f4.f1"] == 5
    assert fields["run_state.f4.f2.f1"] == 7
    assert fields["run_state.f4.f2.f3"] == 1790881200
    assert fields["run_state.f4.f3"] == "bytes(len=6)"
    assert fields["room[1].f21"] == 4
    assert fields["room[1].f22"] == 15
    assert fields["room[1].f61"] == 1790881200
    assert fields["room[1].f23"] == "bytes(len=9)"
    assert fields["room[1].vent[awake].f2"] == 5
    assert fields["room[1].vent[away].f2"] == 0
    assert fields["room[1].device[7].f8.f2"] == 51
    assert fields["room[1].device[7].f10"] == 0
    assert fields["room[1].device[7].prop[38.0.11]"] == {"hex": "02", "uint_le": 2}
    text = json_bytes(result).decode()
    assert SERIAL not in text
    assert ROOM_NAME not in text
    assert "Some text" not in text
    assert "captured_at" in capture


def test_diagnostics_capture_falls_back_to_last_poll():
    result = diagnostics_for(TimeoutError())
    capture = result["capture"]
    assert capture["source"] == "last poll"
    assert capture["error"] == "TimeoutError"
    assert capture["fields"]["run_state.f12"] == 0
    assert capture["fields"]["room[1].device[7].prop[37.0.1]"] == {
        "hex": "85",
        "uint_le": 133,
    }
    assert "attached_devices" in result

