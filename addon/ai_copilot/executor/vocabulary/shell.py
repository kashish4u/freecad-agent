"""
executor/vocabulary/shell.py - implementation of the `shell` command (Phase 7).

Command definition: shared/commands.schema.json -> catalog.shell
  params: target (req, id of the solid to hollow), thickness (req, wall
          thickness in mm), where ('top'/'bottom', default 'top': the face
          left OPEN), face (opt, explicit id like 'Face3'). Units: mm.

Hollows a solid into a thin-walled shell with one face left open, using
Part::Thickness (core Part workbench, principle 2). The open face is resolved
by the EXECUTOR from 'where' (shared select_face_ref helper, same pattern as
ADR 0006/0014: the model never guesses face indices - principle 7).

The thickness value is applied NEGATIVE (inward): the outer dimensions of the
part stay as designed and the walls grow inward, which is what "hollow it out
with 2 mm walls" means in practice.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object, select_face_ref, hide_object
from ._sizes import resolve_size, baseline_for


def shell(doc, params: dict) -> List:
    """Hollow a solid, leaving the chosen face open."""
    target = resolve_object(doc, params["target"])
    # A "thick"/"thin" wall scales to the body being hollowed; units stay absolute.
    baseline = baseline_for(doc, target)
    thickness = resolve_size(params.get("thickness", 0), baseline)
    if thickness <= 0:
        raise ValueError("thickness must be > 0 (the wall thickness in mm)")

    face_ref = select_face_ref(target, params.get("where", "top"),
                               explicit=params.get("face"))

    hollow = doc.addObject("Part::Thickness", "Shell")
    hollow.Faces = (target, (face_ref,))
    hollow.Value = -abs(thickness)   # negative = walls grow INWARD
    hollow.Mode = 0                  # Skin (hollow solid with opening)
    hollow.Join = 2                  # Intersection (clean corners)
    hollow.Intersection = False
    hollow.SelfIntersection = False
    try:
        doc.recompute()
    except Exception:
        pass
    _require_nonempty(hollow, thickness)
    # Part::Thickness does not auto-hide its base via the data API.
    hide_object(target)
    return [hollow]


def _require_nonempty(obj, thickness: float) -> None:
    """Raise if the thickness operation produced an empty/degenerate shape
    (skipped when shape info is absent, e.g. the headless mock)."""
    shape = getattr(obj, "Shape", None)
    solids = getattr(shape, "Solids", None) if shape is not None else None
    if solids is not None and len(solids) == 0:
        raise ValueError(
            f"shelling failed: {thickness} mm walls do not fit this body "
            "(too thick for its smallest dimension?), or the face choice "
            "is not shellable")
