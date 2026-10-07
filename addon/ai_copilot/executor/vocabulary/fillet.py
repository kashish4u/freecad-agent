"""
executor/vocabulary/fillet.py - implementation of the `fillet` command.

Command definition: shared/commands.schema.json -> catalog.fillet
  params: target (id), radius. Units: mm.
  edge selection (one of):
    - where (str): "all" (default) / "top" / "bottom" / "vertical" / "horizontal"
                   -> the executor resolves the real edges itself (ADR 0006);
    - edges (list of edge ids, e.g. "Edge3"): explicit list (backward compatible).

Creates a Part::Fillet that rounds the chosen edges of the target. FreeCAD's
Fillet.Edges takes a list of (edge_index, radius1, radius2) tuples; we apply a
constant-radius fillet. Parametric and reversible.

WHY executor-side selection (ADR 0006): asking a small local model to enumerate
Edge1..Edge12 correctly is the most fragile output it produces. Letting the
executor pick the edges from the real geometry (principle 7: perceive, don't
guess) makes "round all the edges" reliable. The model just sends target+radius.
"""

from __future__ import annotations

from typing import List

from ._common import (
    resolve_object, parse_edge_indices, select_edge_indices, hide_object,
)


def fillet(doc, params: dict) -> List:
    """Apply a fillet (rounding) to the chosen edges of an existing body."""
    target = resolve_object(doc, params["target"])
    radius = float(params["radius"])
    if radius <= 0:
        raise ValueError("fillet radius must be > 0")

    indices = _resolve_edges(target, params)

    # The requested radius may be too large for the geometry it rounds -- e.g. a
    # fillet of radius 3 mm on a 2 mm slab produces invalid geometry on
    # recompute and failed the whole plan. Try the requested radius, then halve
    # it and retry, so the command still rounds the body instead of erroring out
    # and derailing the rest of the plan.
    attempt = radius
    last_error = None
    while attempt > 0.05:
        feat = doc.addObject("Part::Fillet", "Fillet")
        feat.Base = target
        # Constant-radius fillet: (edge index, radius, radius).
        feat.Edges = [(i, attempt, attempt) for i in indices]
        try:
            doc.recompute()
        except Exception as exc:  # noqa: BLE001 - retry with a smaller radius
            last_error = exc
            doc.removeObject(feat.Name)
            attempt = attempt / 2.0
            continue
        shape = getattr(feat, "Shape", None)
        if shape is not None and shape.isValid():
            # The fillet replaces the base visually: hide the original so its
            # sharp edges don't show through the rounded result (see hide_object).
            hide_object(target)
            return [feat]
        doc.removeObject(feat.Name)
        attempt = attempt / 2.0

    # No radius produced a valid fillet (the target likely has no usable edges or
    # degenerate geometry). Fail with a clear message so the plan can adjust
    # (e.g. drop the fillet) instead of silently leaving nothing rounded.
    raise ValueError(
        f"fillet radius {radius} is too large for the target's geometry; "
        f"tried down to {attempt:.2f} mm without a valid result ({last_error})")


def _resolve_edges(target, params: dict) -> List[int]:
    """
    Decide which edges to round. Explicit `edges` win (backward compatible);
    otherwise select them executor-side from `where` (default "all").
    """
    edges = params.get("edges")
    if edges:
        return parse_edge_indices(edges)
    shape = getattr(target, "Shape", None)
    if shape is None:
        raise ValueError(
            f"target '{getattr(target, 'Name', '?')}' has no shape yet; "
            "recompute the document first")
    return select_edge_indices(shape, params.get("where", "all"))
