#!/usr/bin/env python3
"""Local development driver for the Elektron MD/MM emulation.

One entry point for the loop that matters day to day: configure, build, run the
firmware-backed test suite against the ROMs in ``roms/``, launch the standalone
app in an isolated data root with the MCP server on, and measure performance.

Everything runs against ``temp/cmake_dev`` and ``temp/devroot``; your real
``~/Documents`` and installed plugins are never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mcpclient import McpClient, McpError, read_instances  # noqa: E402
import perfrun  # noqa: E402
import scenarios  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD = ROOT / "temp" / "cmake_dev"
DEVROOT = ROOT / "temp" / "devroot"
ROMS = ROOT / "roms"
BASELINES = pathlib.Path(__file__).resolve().parent / "baselines"

VENDOR_FOLDER = "Gearmulator Preview"

FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
MASK64 = (1 << 64) - 1

# Mirrors md::g_mdOs163Fingerprint / g_mmOs132bFingerprint in mdLib/mdtypes.h.
# The loader identifies firmware by fingerprint, never by filename, so the dev
# root can stage the images under any name.
PRODUCTS = {
    "md": {
        "model": "MD",
        "product": "Gearmulator MD",
        "data_folder": "Machinedrum",
        "firmware_env": "GEARMULATOR_MD_FIRMWARE_BIN",
        "fingerprint": 0x33B7C1A9E29F43FD,
        "note": 36,
    },
    "mm": {
        "model": "MM",
        "product": "Gearmulator MM",
        "data_folder": "Monomachine",
        "firmware_env": "GEARMULATOR_MM_FIRMWARE_BIN",
        "fingerprint": 0xE1C1B461B6D0F21B,
        "note": 60,
    },
}

ROM_SIZE = 0x800000

CONFIGURE_ARGS = [
    "-G", "Ninja Multi-Config",
    "-DCMAKE_CONFIGURATION_TYPES=Debug;Release",
    # The root project forces a universal binary. A dev build only ever runs on
    # this host, and dropping the x86_64 slice halves compile and link time.
    "-DCMAKE_OSX_ARCHITECTURES=arm64",
    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    "-DBUILD_TESTING=ON",
    "-Dgearmulator_SYNTH_ELEKTRON=ON",
    "-Dgearmulator_SYNTH_OSIRUS=OFF",
    "-Dgearmulator_SYNTH_OSTIRUS=OFF",
    "-Dgearmulator_SYNTH_VAVRA=OFF",
    "-Dgearmulator_SYNTH_XENIA=OFF",
    "-Dgearmulator_SYNTH_NODALRED2X=OFF",
    "-Dgearmulator_SYNTH_JE8086=OFF",
    "-Dgearmulator_BUILD_JUCEPLUGIN=ON",
    "-Dgearmulator_BUILD_JUCEPLUGIN_Standalone=ON",
    "-Dgearmulator_BUILD_JUCEPLUGIN_VST3=ON",
    "-Dgearmulator_BUILD_JUCEPLUGIN_VST2=OFF",
    "-Dgearmulator_BUILD_JUCEPLUGIN_AU=OFF",
    "-Dgearmulator_BUILD_JUCEPLUGIN_CLAP=OFF",
    "-Dgearmulator_BUILD_JUCEPLUGIN_LV2=OFF",
    "-Dgearmulator_BUILD_FX_PLUGIN=OFF",
    # Match scripts/macos/build_mdmm.sh, which enables both for every shipping
    # build. Measured on an M3 Max these are worth +8.9% throughput and -10.5%
    # callback load on MD, so a dev Release without them profiles a
    # configuration nobody ships. Release-only; Debug builds are unaffected.
    "-DGEARMULATOR_MDMM_APPLE_THINLTO=ON",
    "-DGEARMULATOR_MDMM_APPLE_OPTIMIZE_DSP=ON",
]


# --------------------------------------------------------------------------- utils

def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], env: dict | None = None, cwd: pathlib.Path | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    print(f"+ {printable}", flush=True)
    return subprocess.run([str(c) for c in cmd], env=env, cwd=cwd, check=check)


def fnv1a64(data: bytes) -> int:
    h = FNV_OFFSET
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def find_roms() -> dict[str, pathlib.Path]:
    """Map product key -> ROM path by fingerprint, exactly as the loader does."""
    found: dict[str, pathlib.Path] = {}
    if not ROMS.is_dir():
        return found

    wanted = {cfg["fingerprint"]: key for key, cfg in PRODUCTS.items()}
    for path in sorted(ROMS.glob("*.bin")):
        if path.stat().st_size != ROM_SIZE:
            continue
        key = wanted.get(fnv1a64(path.read_bytes()))
        if key and key not in found:
            found[key] = path
    return found


def firmware_env(roms: dict[str, pathlib.Path]) -> dict[str, str]:
    return {PRODUCTS[k]["firmware_env"]: str(p) for k, p in roms.items()}


def config_type(args) -> str:
    return args.config


def artefact_dir(config: str) -> pathlib.Path:
    return ROOT / "bin" / "plugins" / config


def standalone_app(key: str, config: str) -> pathlib.Path:
    return artefact_dir(config) / "Standalone" / f"{PRODUCTS[key]['product']}.app"


def vst3_bundle(key: str, config: str) -> pathlib.Path:
    return artefact_dir(config) / "VST3" / f"{PRODUCTS[key]['product']}.vst3"


def latency_host(config: str) -> pathlib.Path:
    return BUILD / "source" / "pluginTester" / "latency" / config / "latency_host"


# ----------------------------------------------------------------- hermetic dev root

def stage_devroot(key: str, rom: pathlib.Path, *, enable_mcp: bool,
                  fresh: bool = False, root_key: str | None = None) -> dict[str, str]:
    """Create an isolated HOME + data root for one product and return its env.

    Isolation is what makes repeated runs comparable: firmware, config, NVRAM
    and logs all live under temp/devroot instead of the developer's Documents
    folder, so a run can be wiped without losing real user state.

    `root_key` names the subdirectory under temp/devroot, defaulting to
    `key`. `perf` passes its own (see cmd_perf) so that `run`/`ui` -- which
    mutate or wipe their shared `temp/devroot/<key>` -- cannot invalidate the
    warm NVRAM/factory-flash cache a performance comparison depends on.
    """
    cfg = PRODUCTS[key]
    case = DEVROOT / (root_key or key)
    if fresh and case.exists():
        shutil.rmtree(case)

    home = case / "home"
    data = case / "data"
    product_dir = data / VENDOR_FOLDER / cfg["data_folder"]
    rom_dir = product_dir / "roms"
    config_dir = product_dir / "config"

    for d in (home, rom_dir, config_dir):
        d.mkdir(parents=True, exist_ok=True)

    staged = rom_dir / rom.name
    if not staged.exists() or staged.stat().st_size != rom.stat().st_size:
        for old in rom_dir.glob("*.bin"):
            old.unlink()
        # Hardlink: an 8 MiB copy per product per run is pure waste, and the
        # firmware image is never written to.
        try:
            os.link(rom, staged)
        except OSError:
            shutil.copyfile(rom, staged)

    config_file = config_dir / f"{cfg['product']}.xml"
    config_file.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<PROPERTIES>\n"
        f'  <VALUE name="enableMcpServer" val="{1 if enable_mcp else 0}"/>\n'
        "</PROPERTIES>\n",
        encoding="utf-8")

    env = dict(os.environ)
    # Drop inherited GEARMULATOR_* so a stale shell export cannot silently
    # redirect the run somewhere else.
    for k in [k for k in env if k.startswith("GEARMULATOR_")]:
        del env[k]
    env["HOME"] = str(home)
    env["GEARMULATOR_DATA_ROOT"] = str(data)
    return env


# ------------------------------------------------------------------------ commands

def cmd_doctor(args) -> int:
    ok = True

    print(f"repo            {ROOT}")
    print(f"host            {platform.platform()} / {platform.machine()}")

    for tool in ("cmake", "ninja", "python3"):
        path = shutil.which(tool)
        print(f"{tool:<16}{path or 'MISSING'}")
        if not path and tool != "python3":
            ok = False

    launcher = shutil.which("ccache") or shutil.which("sccache")
    print(f"compiler cache  {launcher or 'none (builds will be slower)'}")

    roms = find_roms()
    for key, cfg in PRODUCTS.items():
        rom = roms.get(key)
        if rom:
            print(f"rom {cfg['model']:<12}{rom}  (fingerprint ok)")
        else:
            print(f"rom {cfg['model']:<12}MISSING from {ROMS} "
                  f"(need the {cfg['fingerprint']:#x} image)")
            ok = False

    cache = BUILD / "CMakeCache.txt"
    print(f"build dir       {BUILD}  {'configured' if cache.is_file() else 'NOT CONFIGURED'}")
    if not cache.is_file():
        ok = False

    for key in PRODUCTS:
        for config in ("Debug", "Release"):
            app = standalone_app(key, config)
            if app.exists():
                print(f"standalone      {config:<8}{app}")
    for config in ("Debug", "Release"):
        host = latency_host(config)
        if host.exists():
            print(f"latency_host    {config:<8}{host}")

    instances = read_instances()
    print(f"mcp instances   {len(instances)} in ~/.gearmulator_mcp.json")

    print()
    print("doctor: OK" if ok else "doctor: PROBLEMS FOUND")
    return 0 if ok else 1


def cmd_configure(args) -> int:
    cmd = ["cmake", "-S", ROOT, "-B", BUILD, *CONFIGURE_ARGS]
    launcher = shutil.which("ccache") or shutil.which("sccache")
    if launcher:
        cmd += [f"-DCMAKE_C_COMPILER_LAUNCHER={launcher}",
                f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}"]
    run(cmd)
    return 0


def cmd_build(args) -> int:
    if not (BUILD / "CMakeCache.txt").is_file():
        cmd_configure(args)
    cmd = ["cmake", "--build", BUILD, "--config", config_type(args), "-j", str(args.jobs)]
    for target in args.targets:
        cmd += ["--target", target]
    run(cmd)
    return 0


def cmd_test(args) -> int:
    # Build first. ctest happily runs whatever binaries are already on disk,
    # so without this a green suite can describe code you edited hours ago.
    # That happened here: a Release run reported 86/86 against objects built
    # two days earlier, which did not contain the changes it was meant to
    # verify. `run` and `ui` learned this lesson separately; `test` is the
    # one that matters most, because it is the command whose output gets
    # quoted as proof.
    if not args.list:
        cmd_build(argparse.Namespace(
            targets=[], jobs=args.jobs, config=getattr(args, "config", None)))

    roms = find_roms()

    # Plugin-level firmware tests construct a real Processor, which resolves
    # its ROM through the product data folder (Tools::getPublicDataFolder),
    # not through the *_FIRMWARE_BIN env vars mdLibTest-level tests read
    # directly. Stage every ROM that was found into one shared root so a
    # single ctest invocation can satisfy both kinds of test -- the two
    # products live under different data-folder names, so staging "mm"
    # after "md" adds to the root instead of disturbing it.
    # enable_mcp=False: ctest runs many executables, possibly in parallel,
    # and they must not race each other for MCP ports. fresh=False: wiping
    # this root on every `dev.py test` invocation would erase NVRAM/flash
    # state that tests may legitimately build up within a run.
    env = dict(os.environ)
    for key, rom in roms.items():
        env = stage_devroot(key, rom, enable_mcp=False, root_key="test")
    env.update(firmware_env(roms))

    if args.firmware:
        missing = [k for k in PRODUCTS if k not in roms]
        if missing:
            fail(f"--firmware requires both ROMs; missing: {', '.join(missing)}")
        # Without this the firmware gates exit 77 and ctest reports a green run
        # that proved nothing.
        env["MD_AUTOMATION_REQUIRE_FIRMWARE"] = "1"
        env["GEARMULATOR_REQUIRE_FIRMWARE_TESTS"] = "1"

    cmd = ["ctest", "--test-dir", BUILD, "-C", config_type(args),
           "--output-on-failure", "--no-tests=error"]
    if args.regex:
        cmd += ["--tests-regex", args.regex]
    if args.jobs > 1:
        cmd += ["-j", str(args.jobs)]
    if args.list:
        cmd += ["-N"]

    result = run(cmd, env=env, check=False)
    return result.returncode


def standalone_target(key: str) -> str:
    return "mdJucePlugin_Standalone" if key == "md" else "mmJucePlugin_Standalone"


def build_standalone(key: str, config: str, jobs: int) -> pathlib.Path:
    """Build the standalone app, then return its bundle path.

    Always build, never just check for existence. An app that exists but
    predates the source is worse than one that is missing: it launches, the
    scenario passes, and the result describes code that is not the code you
    changed. That happened -- a fix wave was "verified" by ui runs against a
    stale Debug build. Ninja is about a second when everything is current.
    """
    run(["cmake", "--build", BUILD, "--config", config,
         "--target", standalone_target(key), "-j", str(jobs)])

    app = standalone_app(key, config)
    if not app.exists():
        fail(f"{app} still missing after building {standalone_target(key)}")
    return app


def cmd_run(args) -> int:
    key = args.product
    roms = find_roms()
    if key not in roms:
        fail(f"no valid {PRODUCTS[key]['model']} ROM in {ROMS}")

    app = build_standalone(key, config_type(args), args.jobs)

    env = stage_devroot(key, roms[key], enable_mcp=not args.no_mcp, fresh=args.fresh)
    binary = app / "Contents" / "MacOS" / PRODUCTS[key]["product"]

    print(f"+ HOME={env['HOME']}")
    print(f"+ GEARMULATOR_DATA_ROOT={env['GEARMULATOR_DATA_ROOT']}")
    proc = subprocess.Popen([str(binary)], env=env)
    print(f"launched pid {proc.pid}")

    if args.no_mcp:
        if args.wait:
            return proc.wait()
        return 0

    try:
        client = McpClient.wait_for(pid=proc.pid, timeout=args.timeout)
    except Exception as e:
        # wait_for's own loop only swallows McpError/OSError; a stale
        # discovery-file entry can still raise ValueError/KeyError out of
        # int(inst["port"]) or json.loads (see mcpclient.find_instance and
        # McpClient.health). Catching broadly here, the same way cmd_ui's
        # try/except/finally does, is what keeps any such failure from
        # leaving this process running with no pid reported.
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        fail(str(e))

    print(f"mcp ready on port {client.port} (pid {proc.pid})")
    print(f"  {sys.argv[0]} mcp get_front_panel --pid {proc.pid}")

    if args.wait:
        return proc.wait()
    return 0


def cmd_mcp(args) -> int:
    arguments = json.loads(args.arguments) if args.arguments else {}
    client = McpClient.connect(name_substring=args.name, pid=args.pid)
    client.initialize()

    if args.tool == "list":
        for tool in client.list_tools():
            print(f"{tool['name']}\n    {tool.get('description', '')}")
        return 0

    result = client.call(args.tool, **arguments)
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))
    return 0


def cmd_panel(args) -> int:
    """Human-readable front panel dump: the LCD as text plus the lit LEDs."""
    client = McpClient.connect(name_substring=args.name, pid=args.pid)
    client.initialize()
    panel = client.call("get_front_panel", lcd=True)

    print(f"{panel['model']}  litPixels={panel['litPixels']} "
          f"tileWrites={panel['tileWrites']} ledCommands={panel['ledCommands']}")

    page = panel.get("page")
    print(f"page: {page['surface']}/{page['layout']} "
          f"encoders={page['activeEncoderMask']:#04x}" if page else "page: unclassified")

    lit_status = [k for k, v in panel["status"].items() if v]
    lit_mode = [k for k, v in panel["mode"].items() if v]
    print(f"status LEDs: {', '.join(lit_status) or '-'}")
    print(f"mode LEDs:   {', '.join(lit_mode) or '-'}")
    print(f"steps:       {panel['steps']}")

    print("-" * panel["lcdWidth"])
    for row in panel["lcd"]:
        print(row)
    print("-" * panel["lcdWidth"])
    return 0


def cmd_ui(args) -> int:
    """Launch the standalone app, run one scenario against it, shut it down."""
    key = args.product
    roms = find_roms()
    if key not in roms:
        fail(f"no valid {PRODUCTS[key]['model']} ROM in {ROMS}")

    app = build_standalone(key, config_type(args), args.jobs)

    env = stage_devroot(key, roms[key], enable_mcp=True, fresh=True)
    binary = app / "Contents" / "MacOS" / PRODUCTS[key]["product"]
    proc = subprocess.Popen([str(binary)], env=env)
    print(f"launched pid {proc.pid}")

    try:
        client = McpClient.wait_for(pid=proc.pid, timeout=args.timeout)
        client.initialize()
        scenarios.SCENARIOS[args.scenario](client, lambda m: print(f"  {m}"))
    except AssertionError as e:
        print(f"FAIL {args.scenario}: {e}", file=sys.stderr)
        return 1
    except (McpError, OSError) as e:
        # OSError also covers urllib.error.URLError (a subclass): a dropped
        # connection or a wait_for() timeout is an infrastructure failure,
        # the same bucket as a rejected MCP call.
        print(f"ERROR {args.scenario}: {e}", file=sys.stderr)
        return 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    print(f"PASS {args.scenario}")
    return 0


# ----------------------------------------------------------------------- performance


def cmd_perf(args) -> int:
    key = args.product
    cfg = PRODUCTS[key]
    config = "Release"  # base.cmake only applies -Ofast to Release; other
                        # configurations produce numbers that mean nothing.

    roms = find_roms()
    if key not in roms:
        fail(f"no valid {cfg['model']} ROM in {ROMS}")

    host = latency_host(config)
    plugin = vst3_bundle(key, config)
    if not host.exists() or not plugin.exists():
        fail(f"missing {host if not host.exists() else plugin}; run: "
             f"dev.py build --config Release --target latency_host "
             f"{'mdJucePlugin_VST3' if key == 'md' else 'mmJucePlugin_VST3'}")

    if args.repeats < 1:
        fail("--repeats must be at least 1")
    if args.check and args.repeats < 2:
        fail("--check needs at least 2 repeats; a single run has no measurable spread")

    problem = perfrun.validate_run(args.scenario, args.rate, args.block, args.seconds)
    if problem:
        fail(problem)

    # perf gets its own data root, isolated from the one `run`/`ui` share:
    # `ui` wipes its root on every launch (fresh=True) and `run` mutates it,
    # either of which would invalidate the warm NVRAM/factory-flash cache a
    # baseline comparison depends on (see stage_devroot's docstring).
    perf_root_key = f"{key}-perf"
    out_dir = DEVROOT / perf_root_key / "perf" / args.mode
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    env = stage_devroot(key, roms[key], enable_mcp=False, root_key=perf_root_key)

    samples = []
    for rep in range(args.repeats):
        prefix = out_dir / f"capture-{rep}"
        cmd = perfrun.host_command(
            host, plugin, prefix, args.mode, args.scenario, args.rate,
            args.block, args.seconds, cfg["note"])
        started = time.monotonic()
        run(cmd, env=env)
        wall = time.monotonic() - started

        record = {"repeat": rep, "wallSeconds": wall,
                  "xRealtime": args.seconds / wall if wall > 0 else 0.0}

        try:
            blocks = perfrun.read_blocks(
                prefix.with_name(prefix.name + ".blocks.csv"))
        except FileNotFoundError as e:
            print(f"warning: {e}", file=sys.stderr)
            blocks = []

        record.update(perfrun.summarize(blocks, args.rate, args.block, args.warmup))

        samples.append(record)
        print(f"  repeat {rep}: {record}")

    report = {
        "schema": "gearmulator-dev-perf-v1",
        "product": cfg["model"],
        "mode": args.mode,
        "scenario": args.scenario,
        "config": config,
        "rate": args.rate,
        "block": args.block,
        "seconds": args.seconds,
        "warmupSeconds": args.warmup,
        "host": platform.platform(),
        "machine": platform.machine(),
        "samples": samples,
    }

    if args.mode == "throughput":
        renders = [s["xRenderOnly"] for s in samples if "xRenderOnly" in s]
        walls = [s["xRealtime"] for s in samples]
        report["xRenderOnlyMedian"] = statistics.median(renders) if renders else None
        report["xRealtimeMedian"] = statistics.median(walls)
        headline = (f"render-only {report['xRenderOnlyMedian']:.3f}x "
                    f"(wall {report['xRealtimeMedian']:.3f}x, includes startup)"
                    if renders else "no callback timings in capture")
    else:
        p50s = [s["loadP50"] for s in samples if "loadP50" in s]
        p99s = [s["loadP99"] for s in samples if "loadP99" in s]
        report["loadP50Median"] = statistics.median(p50s) if p50s else None
        report["loadP99Median"] = statistics.median(p99s) if p99s else None
        report["overrunsTotal"] = sum(s.get("overruns", 0) for s in samples)
        report["startupOverrunsTotal"] = sum(s.get("startupOverruns", 0) for s in samples)
        headline = (f"steady p50 {report['loadP50Median']:.3f} "
                    f"p99 {report['loadP99Median']:.3f} "
                    f"overruns {report['overrunsTotal']} "
                    f"| startup overruns {report['startupOverrunsTotal']} "
                    f"(first {args.warmup}s, excluded)"
                    if p50s else "no callback timings in capture")

    report["spread"] = perfrun.spread(perfrun.headline_values(samples, args.mode))
    print(f"\n{cfg['model']} {args.mode}: {headline} "
          f"[spread {report['spread']:.1%} over {len(samples)} repeats]")

    if args.output:
        pathlib.Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.output}")

    baseline_file = BASELINES / perfrun.baseline_name(
        key, args.mode, report["scenario"], args.rate, args.block)
    if args.save_baseline:
        if report["spread"] > args.max_spread:
            fail(f"repeats spread {report['spread']:.1%}, above the "
                 f"{args.max_spread:.0%} limit: this run is too noisy to "
                 f"become a baseline. Re-run on an idle machine.")
        BASELINES.mkdir(parents=True, exist_ok=True)
        if baseline_file.is_file():
            existing = json.loads(baseline_file.read_text(encoding="utf-8"))
            mismatch = perfrun.config_mismatch(report, existing)
            if mismatch:
                fail(f"baseline {baseline_file.name} was recorded with a "
                     f"different {', '.join(mismatch)}; delete "
                     f"{baseline_file} first if overwriting it is intended")
        baseline_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"saved baseline {baseline_file}")
        return 0

    if args.check:
        if not baseline_file.is_file():
            fail(f"no baseline at {baseline_file}; record one with --save-baseline")
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        mismatch = perfrun.config_mismatch(report, baseline)
        if mismatch:
            fail(f"baseline {baseline_file.name} was recorded with a different "
                 f"{', '.join(mismatch)}; delete {baseline_file} and re-record "
                 f"it with --save-baseline")
        if report["spread"] > args.max_spread:
            fail(f"repeats spread {report['spread']:.1%}, above the "
                 f"{args.max_spread:.0%} limit: these repeats disagree too much "
                 f"to say anything coherent about performance, regardless of "
                 f"tolerance. Re-run on an idle machine.")
        passed, message = perfrun.compare(report, baseline, args.tolerance)
        print(message)
        if passed is None:
            return 1
        if not passed:
            print("REGRESSION: worse than the threshold above")
            return 1
        print("within threshold")
        return 0

    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dev.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add_config(sp):
        sp.add_argument("--config", default="Debug", choices=("Debug", "Release"))
        sp.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)

    sp = sub.add_parser("doctor", help="verify ROMs, toolchain and build dir")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("configure", help="(re)configure temp/cmake_dev")
    sp.set_defaults(func=cmd_configure)

    sp = sub.add_parser("build", help="build targets (configures first if needed)")
    add_config(sp)
    sp.add_argument("--target", dest="targets", action="append", default=[],
                    help="repeatable; default builds everything")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("test", help="run ctest with the ROMs wired in")
    add_config(sp)
    sp.add_argument("-R", "--regex", help="ctest --tests-regex")
    sp.add_argument("--firmware", action="store_true",
                    help="require firmware gates to run: skips become failures")
    sp.add_argument("-N", "--list", action="store_true", help="list tests only")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("run", help="launch the standalone app hermetically")
    add_config(sp)
    sp.add_argument("product", choices=sorted(PRODUCTS))
    sp.add_argument("--no-mcp", action="store_true", help="do not enable the MCP server")
    sp.add_argument("--fresh", action="store_true", help="wipe the dev root first")
    sp.add_argument("--wait", action="store_true", help="block until the app exits")
    sp.add_argument("--timeout", type=float, default=60.0,
                    help="seconds to wait for the MCP server")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("mcp", help="call one MCP tool on a running instance")
    sp.add_argument("tool", help="tool name, or 'list'")
    sp.add_argument("arguments", nargs="?", help="JSON object of tool arguments")
    sp.add_argument("--name", default="", help="match instance by plugin name substring")
    sp.add_argument("--pid", type=int, help="match instance by host pid")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("panel", help="print the decoded LCD and LEDs")
    sp.add_argument("--name", default="", help="match instance by plugin name substring")
    sp.add_argument("--pid", type=int, help="match instance by host pid")
    sp.set_defaults(func=cmd_panel)

    sp = sub.add_parser("ui", help="run a scenario against the standalone app")
    add_config(sp)
    sp.add_argument("product", choices=sorted(PRODUCTS))
    sp.add_argument("scenario", choices=sorted(scenarios.SCENARIOS))
    sp.add_argument("--timeout", type=float, default=90.0,
                    help="seconds to wait for the MCP server")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("perf", help="measure render performance (Release only)")
    sp.add_argument("product", choices=sorted(PRODUCTS))
    sp.add_argument("--mode", default="throughput", choices=("throughput", "paced"))
    sp.add_argument("--rate", type=int, default=48000)
    sp.add_argument("--block", type=int, default=128)
    sp.add_argument("--scenario", default="notes", choices=perfrun.SCENARIOS,
                    help="workload: notes, chords, input (opens 2 input "
                         "channels), or transport")
    # latency_host requires 20 <= seconds <= 600: the floor gives its warm-up
    # window room to matter before the measurement period starts, the
    # ceiling caps how long a single run can be left rendering.
    sp.add_argument("--seconds", type=int, default=20,
                    help="render duration in seconds, between 20 and 600")
    sp.add_argument("--repeats", type=int, default=3)
    # 8s comfortably covers the JIT warm-up on an M3 Max; raise it on slower
    # hardware if startupOverruns keeps leaking into the steady-state window.
    sp.add_argument("--warmup", type=float, default=8.0,
                    help="seconds excluded from the steady-state figures")
    sp.add_argument("--output", help="write the JSON report here")
    sp.add_argument("--save-baseline", action="store_true")
    sp.add_argument("--check", action="store_true", help="compare against the baseline")
    sp.add_argument("--tolerance", type=float, default=0.05)
    sp.add_argument("--max-spread", type=float, default=0.075,
                    help="refuse to compare or save a baseline when repeats "
                         "disagree this much; 0.075 caps the report's own "
                         "contribution to compare's noise bound at "
                         "0.5*7.5%%=3.75%%, which combined with the widest "
                         "committed baseline spread (~2.04%%) keeps that "
                         "bound at ~4.8%%, at or below the default 5%% "
                         "--tolerance, while still admitting the observed "
                         "4.3%%/3.4%% paced captures that motivated raising "
                         "this from the original 3%%")
    sp.set_defaults(func=cmd_perf)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
