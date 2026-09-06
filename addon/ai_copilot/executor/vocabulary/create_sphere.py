"""
executor/vocabulary/create_sphere.py - implementation of the `create_sphere` command.

Command definition: shared/commands.schema.json -> catalog.create_sphere
  params: radius (req), placement [x,y,z] (opt, the centre). Units: mm.

Part::Sphere is a core FreeCAD primitive (no extra workbench, principle 2).
"""

from __future__ import annotations

from typing import List

from ._common import apply_placement


def create_sphere(doc, params: dict) -> List:
    """Create a sphere in document `doc`."""
    radius = float(params["radius"])
    if radius <= 0:
        raise ValueError("radius must be > 0")

    sphere = doc.addObject("Part::Sphere", "Sphere")
    sphere.Radius = radius
    apply_placement(sphere, params.get("placement"))
    return [sphere]
