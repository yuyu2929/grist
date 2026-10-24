"""Test runner: executes every tests/test_*.py file and reports.

Usage: python3 tests/run_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    files = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py"))
    if not files:
        print("no test files found")
        return 1
    failed = []
    for f in files:
        path = os.path.join(HERE, f)
        print(f"\n=== {f} " + "=" * (60 - len(f)))
        r = subprocess.run([sys.executable, path])
        if r.returncode != 0:
            failed.append(f)
    print("\n" + "=" * 64)
    if failed:
        print(f"SUITE FAILED: {len(failed)}/{len(files)} files failed: {failed}")
        return 1
    print(f"SUITE ALL OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
