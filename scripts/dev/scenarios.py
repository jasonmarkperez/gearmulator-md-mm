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
    last_error: McpError | None = None
    while time.monotonic() < deadline:
        try:
            panel = client.call("get_front_panel", lcd=True)
        except McpError as e:
            # get_front_panel is a product-specific tool that registers a
            # few hundred ms after the MCP server starts answering health
            # checks, and it also raises for other reasons (e.g. no device
            # instance yet). Keep retrying either way, but remember why, in
            # case the condition never clears before the deadline.
            last_error = e
            time.sleep(0.25)
            continue
        if panel["litPixels"] > 0:
            break
        time.sleep(0.5)

    assert panel is not None, (
        "no panel response after 60s"
        + (f": {last_error}" if last_error is not None else ""))
    assert panel["litPixels"] > 0, (
        f"LCD still blank after 60s (tileWrites={panel['tileWrites']}, "
        f"panelBytes={panel['panelBytes']})")
    log(f"{panel['model']} drew {panel['litPixels']} pixels, "
        f"{panel['tileWrites']} tile writes")

    info = client.call("get_device_info")
    assert info["valid"], "device reports itself invalid after boot"


def state_roundtrip(client, log) -> None:
    """A DAW-level save, a change, and a restore leave the machine alive.

    This is the path that regressed in 2.1.2 across three products when
    getState used assign() instead of insert() and overwrote the version
    header the Plugin layer had already pushed.

    setStateInformation() returns void and unconditionally reports success:
    a payload the device rejects takes Device::failProjectStateRestore(),
    which only records a status string and leaves the live machine
    untouched. Length-of-encoded-state is not a substitute for proving the
    restore actually landed -- before the factory baseline is captured, the
    encoder always emits a complete flash image, so length stays constant
    across a totally unrelated (or silently rejected) restore. What actually
    proves a restore committed is get_front_panel's hardwareEpoch, which
    Device::commitPreparedState() increments on every committed swap.
    """
    boot(client, log)

    original = client.call("get_plugin_state")["data"]
    assert original, "get_plugin_state returned nothing"
    log(f"saved {len(original)} base64 chars of plugin state")

    client.call("send_note", note=36, velocity=100, duration_ms=200)
    time.sleep(1.0)

    epoch_before = client.call("get_front_panel", lcd=False)["hardwareEpoch"]
    client.call("set_plugin_state", data=original)

    # A committed restore constructs a fresh machine and reboots it; the
    # commit itself -- and that reboot's first drawn frame -- lands on a
    # timer-driven service pass, so both conditions must be polled with a
    # deadline rather than assumed after a fixed sleep.
    deadline = time.monotonic() + 60.0
    panel = None
    while time.monotonic() < deadline:
        panel = client.call("get_front_panel", lcd=False)
        if panel["hardwareEpoch"] > epoch_before and panel["litPixels"] > 0:
            break
        time.sleep(0.5)

    assert panel is not None, "no panel response after restore"
    epoch_after = panel["hardwareEpoch"]
    unmet = []
    if not (epoch_after > epoch_before):
        unmet.append(f"hardwareEpoch did not advance ({epoch_before} -> {epoch_after})")
    if not (panel["litPixels"] > 0):
        unmet.append(f"LCD blank (litPixels={panel['litPixels']})")
    assert not unmet, f"restore not committed after 60s: {'; '.join(unmet)}"

    restored = client.call("get_plugin_state")["data"]
    # Weak signal on its own (see docstring): the hardwareEpoch check above
    # is what actually proves this restore committed. This only additionally
    # guards against a truncated encode.
    assert len(restored) == len(original), (
        f"restored state is {len(restored)} chars, original was {len(original)}")

    info = client.call("get_device_info")
    assert info["valid"], "device is invalid after restoring its own state"

    log(f"state restored (epoch {epoch_before} -> {epoch_after}), "
        f"device still valid with a live panel")


SCENARIOS = {
    "boot": boot,
    "state_roundtrip": state_roundtrip,
}
