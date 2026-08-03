"""TKAP Tools -- the Türkmen-Karahöyük Archaeological Project QGIS toolset.

One installable plugin holding the project's field tools: stratigraphic
phasing, section drawing, and Emlid GNSS point conversion. They ship together
because they share a data model (Stratigraphic Units) and have to stay in step
when that schema changes.

The import lives inside :func:`classFactory` so nothing touches QGIS at module
import time -- that keeps the pure-Python modules testable without QGIS.
"""


def classFactory(iface):  # noqa: N802 -- name fixed by the QGIS plugin API
    from .plugin import TkapToolsPlugin

    return TkapToolsPlugin(iface)
