"""
executor/vocabulary/revolve.py - implementation of the `revolve` command.

Command definition: shared/commands.schema.json -> catalog.revolve
  params: target (req, id of the sketch/profile), angle (opt, default 360),
          axis 'X'/'Y'/'Z' (opt, default 'Z'), base [x,y,z] (opt, default the
          origin - the point the axis passes through). Units: mm / degrees.

Creates a Part::Revolution: the profile swept around the axis makes a solid of
revolution (vase, pulley, knob...). The axis is a model-friendly LETTER resolved
by the executor (ADR 0011); the consumed profile is hidden afterwards, exactly
like extrude does (the sketch lines would show through the solid otherwise).
The engine chains a freshly created sketch into `target` the same way it does
for extrude (ADR 0010): the model does not have to predict 'Sketch001'.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object, resolve_axis, hide_object


def revolve(doc, params: dict) -> List:
    """Revolve a sketch/profile around an axis into a solid."""
    import FreeCAD  # lazy import: available only inside FreeCAD.

    target = resolve_object(doc, params["target"])
    angle = float(params.get("angle", 360.0))
    if angle == 0:
        raise ValueError("angle must be non-zero (default is 360)")
    if abs(angle) > 360:
        raise ValueError("angle cannot exceed 360 degrees")
    axis = resolve_axis(params.get("axis"), default="Z")

    base = params.get("base")
    if base and len(base) >= 3:
        base_v = FreeCAD.Vector(float(base[0]), float(base[1]), float(base[2]))
    else:
        base_v = FreeCAD.Vector(0.0, 0.0, 0.0)

    rev = doc.addObject("Part::Revolution", "Revolve")
    rev.Source = target
    rev.Axis = axis
    rev.Base = base_v
    rev.Angle = abs(angle)
    rev.Solid = True
    try:
        doc.recompute()
    except Exception:
        pass
    _require_solid(rev)
    # Hide the consumed profile (same reason as extrude: the naked sketch
    # would be drawn on top of the new solid).
    hide_object(target)
    return [rev]


def _require_solid(rev) -> None:
    """Raise if the revolution produced no solid (skipped when the shape carries
    no solid info, e.g. the headless mock)."""
    shape = getattr(rev, "Shape", None)
    solids = getattr(shape, "Solids", None) if shape is not None else None
    if solids is not None and len(solids) == 0:
        raise ValueError(
            "the revolution produced no solid; the profile may not be a closed "
            "region, or it crosses the revolution axis")
