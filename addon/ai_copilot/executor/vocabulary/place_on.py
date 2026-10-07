"""
executor/vocabulary/place_on.py - placement-by-reference: rest a part ON another
part's face (phase 3: placement & assembly).

Command: shared/commands.schema.json -> catalog.place_on
  params: target (req); below (req); face ('top' default, or
          bottom/left/right/front/back); gap (optional mm, default 0).

The model says "put the bracket on top of the base plate"; it does NOT compute
where that is. The engine reads the base's real face (centre + outward normal)
from its bounding box, then positions the target so its own extent along that
normal clears the face by `gap`. Only the target's placement changes; its
orientation is left to the caller (rotate it first if needed). This is the
assembly primitive that turns a pile of parts into a stack.
"""

from __future__ import annotations

from typing import List

from ._common import (PLACE_OPPOSITE, place_against, resolve_object)


def place_on(doc, params: dict) -> List:
    """Rest `target` on a named face of `below`, by reading the real geometry."""
    target = resolve_object(doc, params["target"])
    if target is None:
        raise ValueError(f"target '{params['target']}' not found")
    below = resolve_object(doc, params["below"])
    if below is None:
        raise ValueError(f"below '{params['below']}' not found")

    face = str(params.get("face", "top")).strip().lower()

    # The touching face of the target is the opposite of below's face. Stacking
    # keeps the target's own orientation (align=False); use 'mate' to also orient.
    # place_against parses and validates the gap (single source of truth).
    place_against(target, PLACE_OPPOSITE[face], below, face,
                  gap=params.get("gap", 0.0), align=False)
    return [target]
