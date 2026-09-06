"""
executor/vocabulary/loft.py - implementation of the `loft` command (Phase 7).

Command definition: shared/commands.schema.json -> catalog.loft
  params: profiles (req, LIST of sketch/profile ids, in order), solid (opt,
          default true), ruled (opt, default false = smooth). Units: mm.

Creates a Part::Loft blending through the profiles in order (e.g. a square
that morphs into a circle). The profiles are usually sketches made by
create_sketch, spaced apart via their `placement`; the engine rewrites unknown
profile names to the sketches just created (multi-profile chaining, ADR 0021),
so the model does not have to predict 'Sketch001'. The consumed profiles are
hidden afterwards (same reason as extrude/revolve).
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object, hide_object


def loft(doc, params: dict) -> List:
    """Loft a solid (or shell) through an ordered list of profiles."""
    refs = params.get("profiles")
    if not isinstance(refs, list) or len(refs) < 2:
        raise ValueError("loft needs 'profiles': a list of at least 2 "
                         "sketch/profile ids, in order")
    sections = [resolve_object(doc, str(r)) for r in refs]

    lofted = doc.addObject("Part::Loft", "Loft")
    lofted.Sections = sections
    lofted.Solid = bool(params.get("solid", True))
    lofted.Ruled = bool(params.get("ruled", False))
    lofted.Closed = False
    try:
        doc.recompute()
    except Exception:
        pass
    _require_shape(lofted)
    for s in sections:
        hide_object(s)
    return [lofted]


def _require_shape(obj) -> None:
    """Raise if the loft produced no solid when one was requested (skipped when
    solid info is absent, e.g. the headless mock)."""
    shape = getattr(obj, "Shape", None)
    solids = getattr(shape, "Solids", None) if shape is not None else None
    if getattr(obj, "Solid", False) and solids is not None and len(solids) == 0:
        raise ValueError(
            "the loft produced no solid; check that every profile is a single "
            "CLOSED region and that the profiles do not intersect each other")
