"""Facts about TKAP's data that more than one tool needs to agree on.

Deliberately thin, and deliberately free of any ``qgis`` import so the pure
Python modules in each tool stay testable without QGIS present.
"""

from .constants import (
    SITE_CRS_AUTHID,
    SITE_CRS_EPSG,
    SU_FIELD_EMLID_TARGET,
    SU_FIELD_PHASING,
    SU_FIELD_SECTION,
    WGS84_EPSG,
)

__all__ = [
    "SITE_CRS_AUTHID",
    "SITE_CRS_EPSG",
    "SU_FIELD_EMLID_TARGET",
    "SU_FIELD_PHASING",
    "SU_FIELD_SECTION",
    "WGS84_EPSG",
]
