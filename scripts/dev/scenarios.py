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


def wait_for_stable_epoch(client, log, *, since: float | None = None,
                          settle_after: float = 20.0, quiet_seconds: float = 6.0,
                          deadline: float = 40.0) -> int:
    """Wait for hardwareEpoch to stop moving on its own, then return it.

    Observed on this machine: hardwareEpoch performs one automatic,
    non-restore commit within the first couple of seconds after launch, and
    md::AudioPluginAudioProcessor::serviceFactoryInitialization() (Machinedrum
    only; a no-op on Monomachine) can in principle commit a further one --
    it is gated on Hardware::isFactoryFlashReadyForReboot(), which depends on
    emulated CPU cycles and a flash-idle quiet period, not a fixed wall-clock
    delay, so its timing tracks emulation speed (build config, host load)
    rather than landing at a predictable offset. Across repeated `dev.py ui`
    runs during development only the early automatic bump was ever observed
    to actually fire; the factory-flash commit's diagnostic log line never
    printed even across a 90s dedicated trace. That is consistent with, not
    contradicting, `dev.py perf` observing that same log line on a cold data
    root (see doc/dev_workflow.md's "MD ~150 ms lock waits" section): `perf`
    renders continuous audio through latency_host, which is what advances
    the emulated cycles the quiet period is gated on, while a `ui` scenario
    only advances the device via sparse MCP calls with no continuous render
    behind them, so it can leave that gate unreached no matter how long the
    scenario runs. Do not delete this wait on the strength of that: the
    point is not to wait out one named mechanism, it is that
    boot() returns on the first drawn frame, well before *anything* running
    on its own timeline is guaranteed to have settled, and this function
    defends against any such unattributed commit, known or not, by waiting
    for observed quiescence rather than assuming it.

    `since` anchors the floor below to a moment earlier than this function's
    own start -- pass the time the instance was launched (or as close to it
    as the caller has), since the commit being excluded runs on its own
    clock from instantiation, not from whenever this function happens to be
    called. Defaults to this function's own start if the caller has nothing
    earlier.

    `settle_after` (default 20s) is a floor measured from `since`: this
    function never returns before that much time has elapsed since instance
    start, so a commit that hasn't fired yet still gets a chance to.
    `quiet_seconds` (default 6s) is then required with no further change
    once that floor has passed, so a commit landing right at the boundary is
    still caught rather than raced. `deadline` bounds this function's own
    running time, separately from the floor.
    """
    start = time.monotonic()
    floor_from = since if since is not None else start
    end = start + deadline
    last_epoch = None
    last_change = start
    epoch = None
    while True:
        now = time.monotonic()
        panel = client.call("get_front_panel", lcd=False)
        epoch = panel["hardwareEpoch"]
        if epoch != last_epoch:
            if last_epoch is not None:
                log(f"  hardwareEpoch advanced {last_epoch} -> {epoch} at "
                    f"t={now - start:.1f}s while waiting for it to settle "
                    f"(an automatic commit unrelated to the restore under "
                    f"test, not the restore itself)")
            last_epoch = epoch
            last_change = now
        elapsed = now - floor_from
        quiet = now - last_change
        if elapsed >= settle_after and quiet >= quiet_seconds:
            return epoch
        if now >= end:
            raise AssertionError(
                f"hardwareEpoch never settled within {deadline:.0f}s "
                f"(last value {epoch}, quiet for only {quiet:.1f}s of the "
                f"required {quiet_seconds:.0f}s)")
        time.sleep(0.5)


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

    That check is only sound once the pre-restore baseline excludes any
    commit running on its own clock from instantiation, independent of this
    restore (see wait_for_stable_epoch()) -- otherwise a refused restore can
    still show an epoch increase from that unrelated commit and the scenario
    passes for the exact bug class its docstring cites.
    """
    started = time.monotonic()
    boot(client, log)

    original = client.call("get_plugin_state")["data"]
    assert original, "get_plugin_state returned nothing"
    log(f"saved {len(original)} base64 chars of plugin state")

    client.call("send_note", note=36, velocity=100, duration_ms=200)
    time.sleep(1.0)

    # boot() only waits for the first drawn frame, which can land well
    # before any commit running on its own clock since instantiation has
    # settled. Anchor the settle floor to `started`, not to whenever this
    # call happens to run, or capturing epoch_before too early would
    # attribute an unrelated commit to this restore instead.
    epoch_before = wait_for_stable_epoch(client, log, since=started)
    log(f"hardwareEpoch stable at {epoch_before}, starting the restore")
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
