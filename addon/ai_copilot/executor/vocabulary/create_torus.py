"""
executor/vocabulary/create_torus.py - implementation of the `create_torus` command.

Command definition: shared/commands.schema.json -> catalog.create_torus
  params: radius1 (req, ring radius: axis to tube centre), radius2 (req, tube
          radius), placement [x,y,z] (opt, the centre). Units: mm.

Part::Torus is a core FreeCAD primitive (no extra workbench, principle 2). The
tube must be thinner than the ring or the surface self-intersects: we validate
that HERE with a clear message (principle 7) instead of letting the recompute
produce broken geometry.
"""

from __future__ import annotations

from typing import List

from ._common import apply_placement
from ._sizes import resolve_size, baseline_for


def create_torus(doc, params: dict) -> List:
    """Create a torus (ring/donut) in document `doc`."""
    baseline = baseline_for(doc)
    radius1 = resolve_size(params["radius1"], baseline)
    radius2 = resolve_size(params["radius2"], baseline)
    if radius1 <= 0 or radius2 <= 0:
        raise ValueError("radius1 and radius2 must be > 0")
    if radius2 >= radius1:
        raise ValueError(
            "radius2 (the tube) must be SMALLER than radius1 (the ring), "
            "or the torus self-intersects")

    torus = doc.addObject("Part::Torus", "Torus")
    torus.Radius1 = radius1
    torus.Radius2 = radius2
    apply_placement(torus, params.get("placement"))
    return [torus]
