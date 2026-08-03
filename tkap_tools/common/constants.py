"""Project constants shared across the TKAP tools.

Only things that are true of the *project* belong here -- a value that would
have to be changed in more than one tool if the site or the schema changed.
Tool-specific parsing rules and layer names stay in their own tool.
"""

from __future__ import annotations

#: The site's working CRS: WGS 1984 UTM Zone 36N.
#:
#: Emlid exports arrive in it, section drawings measure their traces in it, and
#: the SU layers are stored in it. It was written out separately in three
#: places before the tools were merged (as ``EMLID_WKID`` in the Emlid tool and
#: twice as ``SITE_CRS`` in the section tool), which is exactly the kind of
#: thing that drifts. If TKAP ever works a site in another zone, this is the
#: one line to change.
SITE_CRS_EPSG = 32636
SITE_CRS_AUTHID = f"EPSG:{SITE_CRS_EPSG}"

#: Plain geographic coordinates. Some Emlid exports leave Easting/Northing
#: blank and populate only Longitude/Latitude, which are in this CRS and get
#: reprojected to the site CRS per point.
WGS84_EPSG = 4326

# -- SU number field names -------------------------------------------------
#
# These are NOT interchangeable and are deliberately not unified. Each names
# the SU number on a different layer, and renaming any of them would break
# reading data that already exists:
#
#   SU_FIELD_POINTS_TARGET  the SU polygon layer the Survey Points tool writes
#                           geometry back onto
#   SU_FIELD_PHASING        the master SU polygon layer the phasing tool reads
#   SU_FIELD_SECTION        the section-drawing polygons written into a
#                           section GeoPackage
#
# They live here so the difference is visible in one place instead of being
# three unrelated string literals in three tools.

SU_FIELD_POINTS_TARGET = "SU"
SU_FIELD_PHASING = "sunumber"
SU_FIELD_SECTION = "su_number"

#: The feature number on the project's Features polygon layer -- the Survey
#: Points tool's second target, alongside the SU layer above. Features are
#: recorded and numbered separately from SUs (they carry no ``space_phase``,
#: which is why the phasing tool cannot split them), so they live on their own
#: layer with their own numbering, and F_ points match against this field.
FEATURE_FIELD_POINTS_TARGET = "Feature"
