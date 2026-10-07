"""
executor/vocabulary/_sizes.py - resolve a shape SIZE to a concrete millimetre value.

Feature 2: "all SHAPES sizes keywords should be accepted". The model speaks loosely
("make a big box", "a cylinder two inch tall", "a thin wall"); the executor turns
that into a real number of millimetres BEFORE the create_* / modify_* commands
apply it (principle 7: the executor perceives, the model need not compute).

resolve_size() accepts three kinds of input:
  * a NUMBER            -> already millimetres, returned as-is.
  * a UNIT STRING       -> "10mm", "2 inch", "3cm", "0.5\"", "1.5in", "5 ft"
                           parsed to millimetres (units are ABSOLUTE: a 2 inch
                           bolt is 50.8 mm whether or not anything else exists).
  * an ADJECTIVE        -> "big"/"small"/"thin"/"thick"/"wide"/"tall" ... mapped
                           to a base millimetre value and then SCALED against the
                           geometry it is describing (see baseline_for): a fillet
                           "small" on a 200 mm body is bigger than a "small"
                           fillet on a 30 mm body, but always proportionally small.

The numeric path is identical to the old `float(params[...])`, so every existing
call with a plain number keeps producing exactly the same geometry.
"""

from __future__ import annotations

import re
from typing import List

# --- scaling constants -------------------------------------------------------
# Adjectives are absolute at this baseline. When the thing being sized has real
# geometry, the adjective's base value is multiplied by (baseline / DEFAULT_BASELINE).
DEFAULT_BASELINE_MM = 100.0          # the "nothing to scale against" default
FLOOR_MM = 0.5                       # never resolve to less than this (avoids 0)
CEIL_MM = 5000.0                     # never resolve to more than this
DEFAULT_ADJECTIVE_MM = 50.0          # fallback for an adjective we do not know

# Adjective -> base millimetres (the value it resolves to when baseline == 100 mm).
_ADJECTIVE_BASE = {
    "tiny": 5.0, "minute": 5.0, "little": 12.0, "mini": 15.0, "miniature": 15.0,
    "small": 20.0,
    "medium": 50.0, "mid": 50.0, "average": 50.0, "regular": 50.0, "standard": 50.0,
    "large": 100.0, "big": 100.0,
    "wide": 100.0, "broad": 100.0, "long": 150.0, "tall": 150.0, "deep": 120.0,
    "substantial": 80.0,
    "thick": 20.0, "chunky": 30.0, "fat": 30.0, "heavy": 40.0,
    "huge": 250.0, "enormous": 400.0, "massive": 400.0, "gigantic": 500.0,
    "gargantuan": 750.0,
    "thin": 5.0, "slim": 4.0, "slender": 6.0, "fine": 4.0, "shallow": 8.0,
    "short": 25.0,
}

# Words that intensify the adjective they sit next to (multiply its base).
_INTENSITY = {"very": 1.3, "really": 1.3, "super": 1.4, "extra": 1.5,
              "quite": 1.15, "ultra": 1.4, "mega": 1.4, "pretty": 1.15}

# Unit -> factor to convert to millimetres. Longest keys first so the regex
# captures the full unit (e.g. "inch" before "in"); the trailing quotes handle
# inches (") and feet (').
_UNIT_FACTOR = [
    ("millimetre", 1.0), ("millimetres", 1.0), ("millimeters", 1.0),
    ("millimeters", 1.0), ("millimetre", 1.0), ("mm", 1.0),
    ("cm", 10.0), ("centimetre", 10.0), ("centimetres", 10.0),
    ("centimeter", 10.0), ("centimeters", 10.0),
    ("m", 1000.0), ("metre", 1000.0), ("metres", 1000.0), ("meter", 1000.0),
    ("meters", 1000.0),
    ("um", 0.001), ("µm", 0.001), ("μm", 0.001), ("micron", 0.001),
    ("microns", 0.001), ("nm", 1e-6),
    ("inch", 25.4), ("inches", 25.4), ("in", 25.4), ('"', 25.4),
    ("foot", 304.8), ("feet", 304.8), ("ft", 304.8), ("'", 304.8),
    ("yard", 914.4), ("yards", 914.4),
    ("mile", 1609344.0), ("miles", 1609344.0),
]
_UNIT_RE = re.compile(
    r"^\s*(-?)\s*(\d+(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u, _ in _UNIT_FACTOR) + r")\s*$",
    re.IGNORECASE)


def _parse_units(text: str):
    """
    Parse a UNIT string ("10mm", "2 inch", "0.5\"") to millimetres, or None if the
    text has no recognised unit suffix. Case-insensitive; a leading '-' is kept.
    """
    m = _UNIT_RE.match(text)
    if not m:
        return None
    sign = -1 if m.group(1) else 1
    value = float(m.group(2))
    unit = m.group(3).lower()
    factor = dict(_UNIT_FACTOR).get(unit)
    if factor is None:
        return None
    return sign * value * factor


def _adjective_base(text: str):
    """
    Resolve a space-separated adjective phrase ("very big", "really thin") to a base
    millimetre value, or None if no adjective/intensity word matched. The strongest
    matched adjective sets the magnitude; any intensity word scales it up.
    """
    words = re.split(r"[,\s]+", text)
    base = None
    boost = 1.0
    for w in words:
        if w in _INTENSITY:
            boost *= _INTENSITY[w]
        elif w in _ADJECTIVE_BASE:
            base = w if base is None else max(base, w, key=_ADJECTIVE_BASE.get)
    if base is None:
        return None
    return _ADJECTIVE_BASE[base] * boost


def resolve_size(value, baseline=None) -> float:
    """
    Resolve a shape SIZE to millimetres.

    * number (int/float) -> returned unchanged (millimetres).
    * unit string        -> converted to millimetres (absolute, unscaled).
    * bare number string -> parsed to millimetres.
    * adjective          -> mapped to a base value, then scaled by `baseline`
                            (the largest object's size, or the target's size) and
                            clamped to [FLOOR_MM, CEIL_MM].

    Raises ValueError on a value that is neither a number, a unit string nor an
    adjective (the engine surfaces this to the model for self-correction).
    """
    if value is None:
        raise ValueError("size is required")

    if isinstance(value, bool):            # bool is an int subclass; treat as error
        raise ValueError(f"size must be a number or keyword, got '{value}'")

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty size")

    # Units are absolute: "2 inch" is 50.8 mm no matter what else exists. The
    # magnitude is clamped to [FLOOR_MM, CEIL_MM]; a leading '-' keeps its sign.
    mm = _parse_units(text)
    if mm is not None:
        sign = -1 if mm < 0 else 1
        return sign * min(max(abs(mm), FLOOR_MM), CEIL_MM)

    # Bare number string, e.g. "10" -> 10 mm.
    try:
        return float(text)
    except ValueError:
        pass

    # Adjective, scaled against the geometry it describes.
    base = _adjective_base(text)
    if base is None:
        raise ValueError(
            f"could not understand size '{value}': use a number (mm), a unit "
            "like '10mm' or '2 inch', or a word like 'big'/'small'/'thin'")

    if baseline and baseline > 0:
        factor = baseline / DEFAULT_BASELINE_MM
        factor = min(max(factor, 0.2), 5.0)
        result = base * factor
    else:
        result = base
    return min(max(result, FLOOR_MM), CEIL_MM)


def _bbox_span(obj) -> float:
    """Largest axis length (mm) of an object's bounding box, or 0 if it has none."""
    bb = getattr(obj, "Shape", None)
    bb = getattr(bb, "BoundBox", None) if bb is not None else None
    if bb is None:
        return 0.0
    try:
        return max(bb.XMax - bb.XMin, bb.YMax - bb.YMin, bb.ZMax - bb.ZMin)
    except Exception:
        return 0.0


def baseline_for(doc, target=None) -> float:
    """
    The size (largest axis, mm) to scale adjectives against:
      * a `target` object's own largest axis (edits: a fillet scales to the body
        it rounds), or
      * the largest axis of any object already in `doc` (create: "bigger than
        what exists"), or
      * DEFAULT_BASELINE_MM when nothing has a computed shape yet.

    Robust to the headless mock: objects without a BoundBox simply contribute 0.
    """
    import FreeCAD  # lazy import: available only inside FreeCAD.

    if target is not None:
        span = _bbox_span(target)
        if span > 0:
            return span

    largest = 0.0
    for obj in getattr(doc, "Objects", []):
        span = _bbox_span(obj)
        if span > largest:
            largest = span
    return largest if largest > 0 else DEFAULT_BASELINE_MM
