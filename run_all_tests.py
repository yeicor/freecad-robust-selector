#!/usr/bin/env python3
"""Unified test runner for FreeCAD Robust Selector.

Runs both offline logic tests and live FreeCAD integration tests (including GUI).
"""
import os
import re
import subprocess
import sys
import unittest

# Ensure icons and module imports are discoverable
os.environ["FREECAD_NO_POPUP"] = "1"
os.environ["QT_STYLE_OVERRIDE"] = "Fusion"

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

# Add FreeCAD libraries if available on the system
FREECAD_LIB = "/usr/lib/freecad/lib"
if os.path.isdir(FREECAD_LIB) and FREECAD_LIB not in sys.path:
    sys.path.insert(0, FREECAD_LIB)

# Modules quarantined into their own GUI process.  Their documents undergo
# file save/close/reopen cycles and exotic view-provider teardown (App Links,
# Assembly joints, TechDraw pages, Part dress-ups) that destabilize a shared
# GUI session through upstream Qt/Coin races (flaky Start-timer use-after-free
# on later event pumping).  Each passes deterministically in isolation, and
# realistic multi-cycle user sessions with background planning active are
# clean — so this is test-process isolation, not a product workaround.
# Results are merged below.
# (Matched by trailing component: discover may yield short module names.)
ISOLATED_MODULES = [
    "test_real_freecad_cross_document",
    "test_real_freecad_consumer_compatibility",
    "test_real_freecad_gear_topology_shock",
]


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _split_suite(suite):
    """Partition discovered tests into (in-process, isolated-by-module)."""
    main = unittest.TestSuite()
    isolated: dict[str, list] = {}
    for test in _flatten(suite):
        short = type(test).__module__.split(".")[-1]
        if short in ISOLATED_MODULES:
            isolated.setdefault(short, []).append(test)
        else:
            main.addTest(test)
    return main, isolated


def _run_isolated_module(module):
    """Run one quarantined module in a fresh process; return (ran, bad, output)."""
    env = dict(os.environ)
    env["FREECAD_NO_POPUP"] = "1"
    env["QT_STYLE_OVERRIDE"] = "Fusion"
    path_entries = [WORKSPACE_DIR]
    if os.path.isdir(FREECAD_LIB):
        path_entries.append(FREECAD_LIB)
    if env.get("PYTHONPATH"):
        path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_entries)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", module, "-v"],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = proc.stdout
    ran, bad, skipped = 0, 0, 0
    for line in output.splitlines():
        if line.startswith("Ran "):
            try:
                ran = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("FAILED"):
            numbers = [int(n) for n in re.findall(r"=(\d+)", line)]
            bad = sum(numbers) if numbers else 1
        elif line.startswith("OK"):
            numbers = [int(n) for n in re.findall(r"skipped=(\d+)", line)]
            skipped = sum(numbers) if numbers else 0
    print(output)
    return ran, bad, skipped, proc.returncode


def main():
    print("=" * 76)
    print("  RUNNING COMPLETE FREECAD ROBUST SELECTOR TEST SUITE")
    print("=" * 76)

    # Detect FreeCAD availability
    try:
        import FreeCAD as App
        print(f"FreeCAD Version: {'.'.join(App.Version()[:3])} (Build {App.Version()[3]})")
    except ImportError:
        print("FreeCAD module: NOT INSTALLED (Running offline mock tests only)")

    try:
        import FreeCADGui as Gui
        print("FreeCADGui: AVAILABLE (Enabling real GUI panel & command tests)")
    except ImportError:
        print("FreeCADGui: NOT AVAILABLE (Skipping GUI tests)")

    print("-" * 76)

    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(WORKSPACE_DIR, "tests"), pattern="test_*.py")
    main_suite, isolated = _split_suite(suite)
    if isolated:
        print(f"Quarantined GUI-process isolation: {', '.join(sorted(isolated))}")
    print("-" * 76)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(main_suite)

    total_ran = result.testsRun
    total_bad = len(result.failures) + len(result.errors)
    total_skipped = len(result.skipped)
    success = result.wasSuccessful()
    for module in sorted(isolated):
        print("-" * 76)
        print(f"  ISOLATED MODULE: {module}")
        print("-" * 76)
        ran, bad, skipped, returncode = _run_isolated_module("tests." + module)
        total_ran += ran
        total_bad += bad
        total_skipped += skipped
        if returncode != 0 or bad:
            success = False

    print("=" * 76)
    print(f"Tests run: {total_ran}")
    print(f"Passed:    {total_ran - total_bad}")
    print(f"Skipped:   {total_skipped}")
    print(f"Failed/Errored: {total_bad}")
    print("=" * 76)

    # Use os._exit to bypass PySide6 / Qt6 static deallocator race conditions at libc shutdown
    os._exit(0 if success else 1)


if __name__ == "__main__":
    main()
