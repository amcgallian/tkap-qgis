# -*- coding: utf-8 -*-
"""Emlid to SU.

Builds SU polygons from Emlid GNSS point exports, either replacing geometry on
an existing SU layer or building onto a new temporary layer. The plugin entry
point is the host's -- see :mod:`tkap_tools.plugin`; the class it wants is
:class:`~.emlid_to_su_plugin.EmlidToSuPlugin`.
"""
