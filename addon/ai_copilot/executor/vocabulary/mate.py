"""
executor/vocabulary/mate.py - joint assembly: mate two parts so a face of `a`
touches a face of `b`, normals opposing (phase 3: placement & assembly).

Command: shared/commands.schema.json -> catalog.mate
  params: a (req); b (req); face_a (req); face_b (req); gap (opt, default 0);
          align (opt, default True — also rotate `a` so its face normal points
          away from `b`; False = position only, keep `a`'s orientation).

The general joint: place_on is the special case where `a`'s bottom mates `b`'s
top. Here either face of either part may be the mating pair, and the engine
computes both the position AND (unless align=False) the rotation that turns `a`'s
face normal onto the opposite of `b`'s, so the parts actually face each other.
"""

from __future__ import annotations

from typing import List

from ._common import place_against, resolve_object


def mate(doc, params: dict) -> List:
    """Mate `a`'s face_a against `b`'s face_b (touching, normals opposing)."""
    a = resolve_object(doc, params["a"])
    if a is None:
        raise ValueError(f"a '{params['a']}' not found")
    b = resolve_object(doc, params["b"])
    if b is None:
        raise ValueError(f"b '{params['b']}' not found")

    face_a = str(params["face_a"]).strip().lower()
    face_b = str(params["face_b"]).strip().lower()
    align = bool(params.get("align", True))

    # place_against parses and validates the gap (single source of truth).
    place_against(a, face_a, b, face_b, gap=params.get("gap", 0.0), align=align)
    return [a]
