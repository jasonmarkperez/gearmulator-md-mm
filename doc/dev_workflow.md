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

### Scenario library

```sh
python3 scripts/dev/dev.py ui md boot
python3 scripts/dev/dev.py ui md state_roundtrip
```

Each scenario launches the standalone app in a fresh isolated data root,
drives it over MCP, asserts, and shuts the instance down again. Scenarios live
in `scripts/dev/scenarios.py` and assert on decoded panel state and device
facts, never on screenshots.

`state_roundtrip` covers the DAW save/restore path through the shipping app.
That path regressed in 2.1.2 across three products, and no other local check
exercises it end to end.

## Measure performance

```sh
python3 scripts/dev/dev.py perf md --mode throughput
python3 scripts/dev/dev.py perf md --mode paced
python3 scripts/dev/dev.py perf md --mode throughput --scenario chords
```

Both modes drive `latency_host` (`source/pluginTester/latency/`) against the
Release VST3 in a data root reserved for `perf` alone
(`temp/devroot/<product>-perf`), separate from the one `run` and `ui` use.
That isolation matters: `ui` wipes its root on every launch and `run` mutates
it, either of which would silently turn a warm baseline comparison cold (see
"Fixed: MD ~150 ms lock waits" below). `--seconds` must be between 20 and 600
(default 20); `latency_host` enforces both bounds — the floor so its warm-up
window does not dominate, the ceiling so a run cannot be left rendering
indefinitely.

- **throughput** renders unpaced and reports **render-only** throughput
  (`xRenderOnly`) as the headline figure — seconds of audio per second spent
  actually inside the render callback — with wall-clock throughput
  (`xRealtime`) reported alongside as a secondary figure. Render-only covers
  only the steady-state window, while wall clock also blends in the cheaper
  warm-up phase, so on MD render-only can read slightly *below* wall clock;
  that ordering is expected, not a fault.
- **paced** renders at real time and reports the render-duration distribution as
  a fraction of the callback budget (p50/p99/max) plus overruns. This is the
  "will it crackle" check. Scheduler arrival lateness is tracked separately by
  the host and is not folded into these numbers.
- `--scenario` selects the workload: `notes` (default), `chords`, `input`
  (opens two input channels), or `transport`.

Record and compare baselines:

```sh
python3 scripts/dev/dev.py perf md --mode throughput --save-baseline
python3 scripts/dev/dev.py perf md --mode throughput --check
python3 scripts/dev/dev.py perf md --mode throughput --check --max-spread 0.05
```

Baselines live in `scripts/dev/baselines/` as one file per exact
configuration: the filename is qualified by product, mode, scenario, rate and
block, and the file records the host it was taken on. A `--check` or
`--save-baseline` against a baseline recorded under a different configuration
is refused outright rather than silently compared or overwritten — delete the
stale baseline file first if replacing it is intended. Baselines are
machine-specific, so `--check` is a local gate, not a CI one, and only
slowdowns fail it. `--max-spread` (default 8%) separately refuses the
comparison when the repeats themselves disagree by more than that fraction.
`compare`'s own threshold (below) already widens itself to absorb ordinary
run-to-run noise, so `--max-spread` is not there to protect `--tolerance`
from noise directly — but the noise bound it feeds, `0.5 * (report_spread +
baseline_spread)`, is otherwise unbounded above, and a report spread wide
enough would let a genuine regression hide inside a silently-widened
threshold. 8% caps the report's own contribution at `0.5 * 8% = 4%`, which
combined with the ~2% spread seen on real committed baselines keeps the
noise bound from exceeding the default 5% `--tolerance`; it still admits
the 4.3% and 3.4% paced captures that motivated raising this limit off its
original, too-tight 3%. Repeats disagreeing by more than that are refused
outright as a pathological run rather than compared at all. Expect a few
percent of run-to-run noise; compare on an otherwise idle machine.

`--check`'s threshold is not `--tolerance` alone: it is the larger of
`--tolerance` and the combined observed noise of the two runs being
compared, `0.5 * (report_spread + baseline_spread)`, where each report's
`spread` is the range of its repeats' headline value divided by their
median. A median's uncertainty is roughly half that range, so two medians
are jointly worth about that much noise, and a move smaller than it is not
resolvable — calling it a regression would just be reacting to noise. The
printed comparison line always names which bound applied (`tolerance` or
`noise`) so a passing run that was only noise-limited is visible rather than
silently lenient. Baselines saved before this existed have no `spread` key;
those compare against `--tolerance` alone, since a missing spread is treated
as 0.

This matters most for **paced**: measured `loadP50` spread across separate
`latency_host` invocations has ranged from 0.4% to 4.9% session to session,
and a controlled A/B holding every variable fixed still produced a 1.4%
"change" from no change at all. A fixed 5% paced tolerance sits inside that
noise floor — it can both fire on noise and mask a real sub-5% regression.
Throughput does not have this problem; its spread is consistently around
0.5%. Use more repeats for paced `--check` runs than you would for
throughput, so `spread` — and therefore the noise bound — reflects the
distribution rather than a lucky pair of samples.

### Warm-up window

`--warmup` (default 8s) splits each run. Boot and DSP JIT make the opening
seconds mildly expensive and noisy — measured peak about 2.9x budget — and
folding them into the totals made the overrun count useless: three repeats of
one build configuration produced 55, 86 and 108 overruns. The headline figures
cover the steady state; the window is reported separately as
`startupOverruns` / `startupLoadMax`, never merged in.

### Fixed: MD ~150 ms lock waits around 16 s

Machinedrum, and only Machinedrum, used to show two isolated callbacks of
roughly 150 ms about 16 seconds after instantiation, some 375 ms apart — 57x
over a 128-sample budget, an audible dropout on every first run. MM never
exceeded 1.6x in the same scenario.

It was not JIT. A capture with `GEARMULATOR_RT_INSTRUMENTATION=1` attributed
it to the device lock:

```
t=16.314s dur=155.65ms device=1.39ms lockWait=154.18ms jitLive=0
t=16.695s dur=143.57ms device=1.12ms lockWait=142.38ms jitLive=24
```

Emulation took 1.4 ms; the rest was waiting. JIT-heavy callbacks peak at
5.8 ms and were unrelated.

`AudioPluginAudioProcessor::serviceFactoryInitialization()` already kept cache
encoding, filesystem writes and replacement construction outside the lock, but
both of its locked sections called `Device::getState(StateTypeGlobal)`. Direct
measurement showed why that mattered: copying the 9 MiB of patch RAM and flash
took **0.26 ms**, while encoding it took **148 ms**. The second call existed
only to detect concurrent modification, by comparing the whole re-encoded blob.

`md::Device` now separates the two halves. `captureStateInputs()` copies the
raw inputs and is what runs under the lock; `encodeStateInputs()` is pure and
runs after the lock is released. `getState()` is implemented as capture plus
encode, so there is still one encoding path. The commit guard compares two
captures instead of two encoded blobs — same guarantee, ~0.3 ms instead of
~150 ms held against the audio thread.

Result: MD `loadMax` fell from 57.7 to 1.70–1.80, and `dev.py perf` still logs
`[MD] factory flash preparation complete; rebooted in process` on a cold
data root, so the work still happens. That observation is specific to
`perf`: it renders continuous audio through `latency_host`. A `dev.py ui` scenario
does not emit the factory-flash reboot log line, while `dev.py perf` does. The cause
is not established. See `wait_for_stable_epoch()`'s docstring in `scenarios.py` for
how this is observed and defended against. Neither claim is stale; they
describe two different data roots and workloads. A state-generation counter
was considered and rejected: patch RAM is written from the CPU store path,
so tracking it would tax the hot emulation loop to solve a problem that
costs nothing to solve this way.

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
