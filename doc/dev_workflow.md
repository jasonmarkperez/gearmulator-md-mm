# MD/MM development workflow

`scripts/dev/dev.py` is the local loop for working on the Elektron Machinedrum
and Monomachine emulation: configure, build, run the firmware-backed tests
against the ROMs in `roms/`, drive the standalone app, and measure performance.

It is a developer tool, not release tooling. Producing a shippable macOS package
is still `scripts/macos/build_mdmm.sh`, which cleans its own roots, signs,
validates the optimization cache and writes a receipt.

## Start here

```sh
python3 scripts/dev/dev.py doctor
```

`doctor` fingerprints every `roms/*.bin` with the same FNV-1a the loader uses,
so it reports the MD OS 1.63 and MM OS 1.32b images by identity rather than by
filename. It also reports the toolchain, whether a compiler cache is present,
and which artefacts already exist.

## Build

```sh
python3 scripts/dev/dev.py configure                      # temp/cmake_dev
python3 scripts/dev/dev.py build                          # Debug, everything
python3 scripts/dev/dev.py build --config Release --target mdJucePlugin_VST3
```

The dev configuration deliberately differs from the release one:

| Setting | Dev | Why |
|---|---|---|
| Generator | Ninja Multi-Config | `Debug` and `Release` out of one configure |
| `CMAKE_OSX_ARCHITECTURES` | `arm64` | the root project forces universal; the x86_64 slice is dead weight on a dev box |
| Synths | Elektron only | the other six emulations are not being worked on |
| Formats | Standalone + VST3 | Standalone is the surface under test, VST3 is what `latency_host` measures |
| `GEARMULATOR_MDMM_APPLE_THINLTO` + `_OPTIMIZE_DSP` | `ON` | both are on for every shipping build (`scripts/macos/build_mdmm.sh`); without them a dev Release profiles a configuration nobody ships |

Use `Debug` for compile checks. Use `Release` for anything timed: `base.cmake`
applies `-Ofast -funroll-loops` only to `Release`, so timings from any other
configuration are meaningless. The two Apple optimization options are also
Release-only, and worth roughly 9% throughput on MD — see
`doc/mdmm-apple-optimization.md` for the measurements.

## Test

```sh
python3 scripts/dev/dev.py test --config Release --firmware
python3 scripts/dev/dev.py test --config Release -R '^md(State|Flash)Test$'
```

Every firmware-backed gate reads `GEARMULATOR_MD_FIRMWARE_BIN` /
`GEARMULATOR_MM_FIRMWARE_BIN` and returns CTest skip code 77 when they are
unset. A plain `ctest` therefore reports a green run that proved almost nothing.
`dev.py test` exports both paths from `roms/`, and `--firmware` additionally
sets `MD_AUTOMATION_REQUIRE_FIRMWARE=1` so a missing image becomes a failure
instead of a skip.

## Run the standalone app

```sh
python3 scripts/dev/dev.py run md --fresh
```

The app launches with `HOME` and `GEARMULATOR_DATA_ROOT` pointed at
`temp/devroot/<product>/`, so firmware, config, NVRAM and logs are isolated from
your real `~/Documents` and from any installed build. The ROM is hardlinked into
the dev root rather than copied. `--fresh` wipes the root first, which is how you
reproduce first-launch behaviour.

MCP is enabled in the staged config, so the app registers itself in
`~/.gearmulator_mcp.json` and `run` waits until the server answers before
returning. `--no-mcp` opts out.

## Drive it

```sh
python3 scripts/dev/dev.py mcp list
python3 scripts/dev/dev.py mcp send_note '{"note": 36, "velocity": 100}'
python3 scripts/dev/dev.py mcp click_element '{"selector": ".trig1"}'
python3 scripts/dev/dev.py panel
```

`panel` prints the decoded front panel: the 128x64 LCD as ASCII, the classified
LCD page, and the lit status/mode/step/drum LEDs. It is the readable form of the
`get_front_panel` MCP tool (see `doc/mcp_server.md`).

Assert on panel state, not on screenshots. The LCD is a rendered bitmap, so the
RmlUi DOM says nothing about its contents, and screenshot comparisons break on
renderer, HiDPI and skin changes. `get_front_panel` reports what the firmware
actually drew.

`scripts/dev/mcpclient.py` is importable if you want to write a scenario in
Python rather than issue one call at a time:

```python
from mcpclient import McpClient

client = McpClient.wait_for(name_substring="MD")
client.initialize()
panel = client.call("get_front_panel")
assert panel["litPixels"] > 0
```

When several instances are running, disambiguate with `--pid` (or `pid=`); the
client verifies liveness by pid, because the discovery file keeps stale entries
after a crash.

## Measure performance

```sh
python3 scripts/dev/dev.py perf md --mode throughput
python3 scripts/dev/dev.py perf md --mode paced
```

Both modes drive `latency_host` (`source/pluginTester/latency/`) against the
Release VST3 in an isolated data root. Minimum run length is 20 seconds, which
the host enforces so its warm-up window does not dominate.

- **throughput** renders unpaced and reports `xRealtime` — seconds of audio per
  second of wall clock. This is the cheap A/B for emulation, DSP and JIT work.
- **paced** renders at real time and reports the render-duration distribution as
  a fraction of the callback budget (p50/p99/max) plus overruns. This is the
  "will it crackle" check. Scheduler arrival lateness is tracked separately by
  the host and is not folded into these numbers.

Record and compare baselines:

```sh
python3 scripts/dev/dev.py perf md --mode throughput --save-baseline
python3 scripts/dev/dev.py perf md --mode throughput --check
```

Baselines live in `scripts/dev/baselines/` and record the host they were taken
on. They are machine-specific, so `--check` is a local gate, not a CI one — and
only slowdowns fail it. Expect a few percent of run-to-run noise; compare on an
otherwise idle machine.

The first callbacks of any run include JIT compilation and show enormous load
values. That is why the headline numbers are medians, and why `loadMax` on its
own is not a regression signal.

## Promoting a scenario into a gate

A scenario that proves useful in the driver should become a test, at the level
it can actually run:

- **Panel and LCD semantics** belong in an in-process test in the
  `source/elektron/md/mdLibTest/` or `mdJucePlugin/` style — drive `md::Hardware`,
  tap `md::PanelControl`, assert on the `FrontPanel` snapshot
  (`mmLcdEditPagesFirmwareTest.cpp` is the model). These are deterministic, fast,
  and run in the Linux CI gates.
- **Standalone-app scenarios** stay local. They need a window server and a built
  `.app`, which the hosted Linux gates cannot provide.
- **Performance baselines** stay local, for the reason above.
