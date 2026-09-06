"""
executor/vocabulary/create_cone.py - implementation of the `create_cone` command.

Command definition: shared/commands.schema.json -> catalog.create_cone
  params: radius1 (req, base), radius2 (opt, top, default 0 = pointed cone),
          height (req), placement [x,y,z] (opt). Units: mm.

Part::Cone is a core FreeCAD primitive (no extra workbench, principle 2). With
radius2 > 0 it is a frustum (truncated cone).
"""

from __future__ import annotations

from typing import List

from ._common import apply_placement


def create_cone(doc, params: dict) -> List:
    """Create a cone (or truncated cone) in document `doc`."""
    radius1 = float(params["radius1"])
    radius2 = float(params.get("radius2", 0.0))
    height = float(params["height"])
    if radius1 <= 0 or height <= 0:
        raise ValueError("radius1 and height must be > 0")
    if radius2 < 0:
        raise ValueError("radius2 must be >= 0 (0 = pointed cone)")
    if radius1 == radius2:
        raise ValueError(
            "radius1 and radius2 are equal: that is a cylinder - "
            "use create_cylinder instead")

    cone = doc.addObject("Part::Cone", "Cone")
    cone.Radius1 = radius1
    cone.Radius2 = radius2
    cone.Height = height
    apply_placement(cone, params.get("placement"))
    return [cone]
