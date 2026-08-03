"""Generate the QGIS custom-repository feed that QGIS polls for updates.

    python build_repo_xml.py

Writes ``docs/plugins.xml``, which is committed and served by GitHub Pages.
Field machines have its URL in Settings -> Plugins -> Plugin Repositories, and
QGIS then offers every new release as a normal update.

Run it whenever you bump ``version`` in metadata.txt, and commit the result --
Pages deploys straight from the branch, so what is committed is what QGIS reads.

Everything is derived from ``tkap_tools/metadata.txt``, including the download
URL, which is pinned to the matching ``v<version>`` tag rather than a
"latest release" permalink. That is deliberate: a permalink stays valid while
pointing at the *previous* zip, so a feed published before its release would
hand out the old plugin while advertising the new version number, and the
installed version would then match the feed and never update again. A pinned
URL 404s instead, which is visible and fixable.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import pathlib
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).parent
METADATA = ROOT / "tkap_tools" / "metadata.txt"
OUT = ROOT / "docs" / "plugins.xml"
REPO_URL = "https://github.com/amcgallian/tkap-qgis"

# metadata.txt key -> element name in the repository XML. The two vocabularies
# differ; this is the whole mapping.
FIELDS = (
    ("description", "description"),
    ("about", "about"),
    ("version", "version"),
    ("qgisMinimumVersion", "qgis_minimum_version"),
    ("qgisMaximumVersion", "qgis_maximum_version"),
    ("homepage", "homepage"),
    ("author", "author_name"),
    ("email", "author_email"),
    ("tracker", "tracker"),
    ("repository", "repository"),
    ("tags", "tags"),
    ("experimental", "experimental"),
    ("deprecated", "deprecated"),
)


def unwrap(value: str) -> str:
    """Turn a metadata.txt multi-line value into plain paragraphs.

    metadata.txt indents continuation lines, and marks a blank line with a line
    holding a single ``.`` -- an ini file cannot carry a genuinely empty
    continuation line. Both are formatting, not content: collapsing the
    indentation without translating the dots leaves " . " sitting mid-sentence
    in what Plugin Manager shows people.
    """
    paragraphs, current = [], []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped == ".":
            if current:
                paragraphs.append(" ".join(current))
                current = []
        elif stripped:
            current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def default_download_url(file_name: str) -> str:
    """The release asset for exactly the version metadata.txt declares."""
    version = read_metadata()["version"]
    return f"{REPO_URL}/releases/download/v{version}/{file_name}"


def read_metadata():
    if not METADATA.is_file():
        sys.exit(f"error: {METADATA} not found")
    parser = configparser.ConfigParser()
    parser.read(METADATA, encoding="utf-8")
    return parser["general"]


def build(download_url: str, file_name: str) -> ET.ElementTree:
    general = read_metadata()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")

    plugins = ET.Element("plugins")
    plugin = ET.SubElement(
        plugins,
        "pyqgis_plugin",
        {"name": general["name"], "version": general["version"]},
    )

    for key, tag in FIELDS:
        value = general.get(key)
        if value:
            ET.SubElement(plugin, tag).text = unwrap(value)

    ET.SubElement(plugin, "file_name").text = file_name
    ET.SubElement(plugin, "download_url").text = download_url
    ET.SubElement(plugin, "uploaded_by").text = general.get("author", "")
    ET.SubElement(plugin, "create_date").text = now
    ET.SubElement(plugin, "update_date").text = now

    tree = ET.ElementTree(plugins)
    ET.indent(tree, space="  ")
    return tree


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--download-url",
        help="override the release-zip URL (default: the v<version> release asset)",
    )
    parser.add_argument("--file-name", default="tkap_tools.zip")
    args = parser.parse_args()

    download_url = args.download_url or default_download_url(args.file_name)
    tree = build(download_url, args.file_name)
    OUT.parent.mkdir(exist_ok=True)
    tree.write(OUT, encoding="utf-8", xml_declaration=True)

    general = read_metadata()
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {general['name']} v{general['version']} -> {download_url}")
