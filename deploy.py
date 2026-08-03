"""Copy TKAP Tools straight into your QGIS profile, skipping the zip entirely.

    python deploy.py                 # default profile
    python deploy.py --profile foo   # a named profile
    python deploy.py --list          # show the profiles found

This is the development loop: run it, then reload the plugin in QGIS (the Plugin
Reloader plugin, or restart QGIS). No version bump, no zip, no Install from ZIP
dialog. Use build_zip.py when you are actually cutting a release.

The install directory is removed and rewritten each time, so a file you deleted
here does not linger there -- a stale module left behind in the profile is the
kind of thing that makes a bug reproduce on your machine and nowhere else.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).parent
SRC = ROOT / "tkap_tools"
PACKAGE = SRC.name

SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}


def profiles_root() -> pathlib.Path:
    """Where QGIS keeps its profiles on this platform."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if not base:
            sys.exit("error: APPDATA is not set; cannot find the QGIS profile")
        return pathlib.Path(base) / "QGIS" / "QGIS3" / "profiles"
    if sys.platform == "darwin":
        return (
            pathlib.Path.home()
            / "Library"
            / "Application Support"
            / "QGIS"
            / "QGIS3"
            / "profiles"
        )
    return pathlib.Path.home() / ".local" / "share" / "QGIS" / "QGIS3" / "profiles"


def list_profiles() -> list[str]:
    root = profiles_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def ignore(_dir, names):
    return [
        n
        for n in names
        if n in SKIP_DIRS or pathlib.PurePath(n).suffix in SKIP_SUFFIXES
    ]


def deploy(profile: str) -> pathlib.Path:
    if not SRC.is_dir():
        sys.exit(f"error: {SRC} not found")

    target_root = profiles_root() / profile / "python" / "plugins"
    if not target_root.parent.is_dir():
        available = ", ".join(list_profiles()) or "none found"
        sys.exit(
            f"error: no QGIS profile called '{profile}'\n"
            f"       looked in {profiles_root()}\n"
            f"       profiles: {available}"
        )
    target_root.mkdir(parents=True, exist_ok=True)

    target = target_root / PACKAGE
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SRC, target, ignore=ignore)

    licence = ROOT / "LICENSE"
    if licence.exists():
        shutil.copy2(licence, target / "LICENSE")

    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="default", help="QGIS profile name")
    parser.add_argument(
        "--list", action="store_true", help="list the QGIS profiles found and exit"
    )
    args = parser.parse_args()

    if args.list:
        found = list_profiles()
        print(f"profiles under {profiles_root()}:")
        for name in found or ["  (none found)"]:
            print(f"  {name}")
        raise SystemExit(0)

    installed = deploy(args.profile)
    count = sum(1 for p in installed.rglob("*") if p.is_file())
    print(f"deployed {count} files to {installed}")
    print("\nNow reload in QGIS: Plugin Reloader, or restart QGIS.")
