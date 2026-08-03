# -*- coding: utf-8 -*-
"""Survey Points to Polygons.

Builds SU and Feature polygons from Emlid GNSS point exports, either replacing
geometry on the existing SU/Features layers or building onto new temporary
layers. The plugin entry point is the host's -- see :mod:`tkap_tools.plugin`;
the class it wants is :class:`~.plugin.SurveyPointsPlugin`.
"""
