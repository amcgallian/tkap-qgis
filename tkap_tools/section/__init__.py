"""TKAP Section Drawing.

Draws archaeological section (vertical wall) drawings by rectifying a photo of
the wall into a section-local coordinate space where x is chainage along the
section trace and y is absolute elevation, then digitising SU polygons over it.

Nothing is imported here, so the pure-Python modules (``section_geom``,
``photo``) stay importable and testable without QGIS present. The plugin entry
point is the host's -- see :mod:`tkap_tools.plugin`; the class it wants is
:class:`~.plugin.TkapSectionPlugin`.
"""
