"""Package TKAP Tools for QGIS 'Install from ZIP' and for a release.

    python build_zip.py

Produces ``dist/tkap_tools.zip`` with a single top-level ``tkap_tools/`` folder,
which is what the QGIS plugin installer expects. Validates the result before
declaring success -- a zip that installs but then fails to load is worse than a
build error, because it fails on someone else's laptop in the field.
"""

from __future__ import annotations

import configparser
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).parent
SRC = ROOT / "tkap_tools"
DIST = ROOT / "dist"
OUT = DIST / "tkap_tools.zip"

PACKAGE = SRC.name

SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
REQUIRED_KEYS = (
    "name",
    "qgisMinimumVersion",
    "description",
    "version",
    "author",
    "email",
)
# Every tool has to be in the zip. Shipping a build that quietly lost one is the
# specific failure this catches: the plugin would install, load, and simply be
# missing a menu entry, which nobody notices until they need that tool.
REQUIRED_TOOLS = ("emlid", "phasing", "section", "common")


def collect():
    return sorted(
        p
        for p in SRC.rglob("*")
        if p.is_file()
        and not SKIP_DIRS.intersection(p.parts)
        and p.suffix not in SKIP_SUFFIXES
    )


def build():
    if not SRC.is_dir():
        sys.exit(f"error: {SRC} not found")

    files = collect()
    DIST.mkdir(exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
        # The licence sits at the repo root, but it has to travel with the
        # plugin: the zip is what people actually receive.
        licence = ROOT / "LICENSE"
        if licence.exists():
            archive.write(licence, f"{PACKAGE}/LICENSE")
    return files


def validate():
    archive = zipfile.ZipFile(OUT)
    names = archive.namelist()

    tops = {n.split("/")[0] for n in names}
    if len(tops) != 1:
        sys.exit(f"error: expected one top-level folder, got {tops}")

    for required in (f"{PACKAGE}/metadata.txt", f"{PACKAGE}/__init__.py"):
        if required not in names:
            sys.exit(f"error: missing {required}")

    for tool in REQUIRED_TOOLS:
        if f"{PACKAGE}/{tool}/__init__.py" not in names:
            sys.exit(f"error: tool '{tool}' is missing from the zip")

    parser = configparser.ConfigParser()
    parser.read_string(archive.read(f"{PACKAGE}/metadata.txt").decode("utf-8"))
    general = parser["general"]
    for key in REQUIRED_KEYS:
        if not general.get(key):
            sys.exit(f"error: metadata.txt missing '{key}'")

    init = archive.read(f"{PACKAGE}/__init__.py").decode("utf-8")
    if "def classFactory" not in init:
        sys.exit("error: __init__.py has no classFactory()")

    return general


if __name__ == "__main__":
    files = build()
    general = validate()
    print(f"{OUT.relative_to(ROOT)}  ({OUT.stat().st_size:,} bytes, {len(files)} files)")
    print(f"  {general['name']} v{general['version']}")
    print(f"  requires QGIS >= {general['qgisMinimumVersion']}")
    print(f"  tools: {', '.join(REQUIRED_TOOLS)}")
    print("\nInstall: Plugins -> Manage and Install Plugins -> Install from ZIP")
