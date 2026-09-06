"""
executor/vocabulary/sweep.py - implementation of the `sweep` command (Phase 7).

Command definition: shared/commands.schema.json -> catalog.sweep
  params: profile (req, id of the cross-section sketch), path (req, id of the
          sketch/wire to sweep along), solid (opt, default true), frenet (opt,
          default true = the profile follows the path's curvature). Units: mm.

Creates a Part::Sweep: the profile is swept along the path (e.g. a circle along
a curved polyline = a pipe). Convention for the model (enforced by the few-shot
examples): draw the PROFILE first, then the PATH; the engine's multi-profile
chaining (ADR 0021) rewrites unknown names in that order. Both consumed inputs
are hidden afterwards.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object, hide_object


def sweep(doc, params: dict) -> List:
    """Sweep a profile along a path into a solid (or shell)."""
    profile = resolve_object(doc, str(params.get("profile") or ""))
    path = resolve_object(doc, str(params.get("path") or ""))
    if profile is path:
        raise ValueError("sweep needs two DIFFERENT sketches: a profile and "
                         "a path")

    swept = doc.addObject("Part::Sweep", "Sweep")
    swept.Sections = [profile]
    # The whole path object is the spine (no sub-edge picking by the model -
    # principle 7; power users can refine in the GUI afterwards).
    swept.Spine = (path, [])
    swept.Solid = bool(params.get("solid", True))
    swept.Frenet = bool(params.get("frenet", True))
    try:
        doc.recompute()
    except Exception:
        pass
    _require_shape(swept)
    hide_object(profile)
    hide_object(path)
    return [swept]


def _require_shape(obj) -> None:
    """Raise if the sweep produced no solid when one was requested (skipped when
    solid info is absent, e.g. the headless mock)."""
    shape = getattr(obj, "Shape", None)
    solids = getattr(shape, "Solids", None) if shape is not None else None
    if getattr(obj, "Solid", False) and solids is not None and len(solids) == 0:
        raise ValueError(
            "the sweep produced no solid; check that the profile is a CLOSED "
            "region and the path is a connected wire that does not "
            "self-intersect")
