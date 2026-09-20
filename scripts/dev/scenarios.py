#!/usr/bin/env python3
"""Named scenarios driven against a running standalone instance over MCP.

Each scenario takes a connected McpClient and a log callable, and raises
AssertionError on failure. Assertions target decoded panel state and device
facts, never screenshots: the LCD is a rendered bitmap, so the RmlUi DOM
carries none of its content and pixel comparisons break on renderer, HiDPI
and skin changes.
"""

from __future__ import annotations

import time

from mcpclient import McpError


def boot(client, log) -> None:
    """The machine boots far enough to draw its screen."""
    deadline = time.monotonic() + 60.0
    panel = None
    while time.monotonic() < deadline:
        try:
            panel = client.call("get_front_panel", lcd=True)
        except McpError:
            # get_front_panel is a product-specific tool that registers a
            # few hundred ms after the MCP server starts answering health
            # checks; treat "not registered yet" the same as "not lit yet".
            time.sleep(0.25)
            continue
        if panel["litPixels"] > 0:
            break
        time.sleep(0.5)

    assert panel is not None, "no panel response"
    assert panel["litPixels"] > 0, (
        f"LCD still blank after 60s (tileWrites={panel['tileWrites']}, "
        f"panelBytes={panel['panelBytes']})")
    assert panel["lcdHeight"] == 64 and panel["lcdWidth"] == 128, (
        f"unexpected LCD geometry {panel['lcdWidth']}x{panel['lcdHeight']}")
    assert len(panel["lcd"]) == 64, f"got {len(panel['lcd'])} LCD rows"
    log(f"{panel['model']} drew {panel['litPixels']} pixels, "
        f"{panel['tileWrites']} tile writes")

    info = client.call("get_device_info")
    assert info["valid"], "device reports itself invalid after boot"


def state_roundtrip(client, log) -> None:
    """A DAW-level save, a change, and a restore leave the machine alive.

    This is the path that regressed in 2.1.2 across three products when
    getState used assign() instead of insert() and overwrote the version
    header the Plugin layer had already pushed.
    """
    boot(client, log)

    original = client.call("get_plugin_state")["data"]
    assert original, "get_plugin_state returned nothing"
    log(f"saved {len(original)} base64 chars of plugin state")

    client.call("send_note", note=36, velocity=100, duration_ms=200)
    time.sleep(1.0)

    client.call("set_plugin_state", data=original)
    time.sleep(2.0)

    restored = client.call("get_plugin_state")["data"]
    # Measured on this machine: a save/change/restore round trip is NOT byte
    # identical (base64 payload differs while its length matches). Keep the
    # length check and the liveness checks; do not assert byte equality.
    assert len(restored) == len(original), (
        f"restored state is {len(restored)} chars, original was {len(original)}")

    info = client.call("get_device_info")
    assert info["valid"], "device is invalid after restoring its own state"

    panel = client.call("get_front_panel", lcd=False)
    assert panel["litPixels"] > 0, "LCD went blank after restore"
    log("state restored, device still valid with a live panel")


SCENARIOS = {
    "boot": boot,
    "state_roundtrip": state_roundtrip,
}
