"""
executor/vocabulary/create_sketch.py - implementation of the `create_sketch` command.

Command definition: shared/commands.schema.json -> catalog.create_sketch
  params: shape ('rectangle'|'circle'|'polygon'|'slot'|'polyline', required),
          plane ('XY'|'XZ'|'YZ', default XY), width/height (rectangle),
          radius (circle: radius; polygon: circumradius), sides (polygon),
          length (slot: overall length; width = slot width), points (polyline),
          placement [x,y,z] (optional). Units: mm.

Phase 7 (schema v0.7.0) added the RICH profiles:
  - polygon:  a regular polygon with `sides` edges inscribed in `radius`.
  - slot:     a rounded slot (asola): overall `length` x `width`, two straight
              edges + two semicircular ARC ends.
  - polyline: a CLOSED wire through `points` [[x,y], ...]; a point may carry a
              third number [x, y, sagitta] to turn the edge TO THE NEXT point
              into an ARC bulging by that height (positive = left of travel).
              This is the executor-side arc support: the maths (circle through
              chord + sagitta) lives HERE, never in the model (principle 7).

Why a REAL Sketcher sketch (see ADR 0009): this unlocks `extrude`, the canonical
FreeCAD "sketch -> solid" workflow, and leaves the user an editable sketch they can
open in the Sketcher workbench. The profile is drawn in the sketch's LOCAL plane
(z = 0) and the sketch is oriented onto the chosen standard plane via its Placement.

  - rectangle: four closed Part.LineSegment edges (corner at the local origin),
    plus coincidence constraints so the wire is a clean closed loop that extrude
    can cap into a solid, and so the sketch stays properly constrained for editing.
  - circle:    one Part.Circle centred on the local origin.

Shape-specific parameters (width/height vs radius) are validated HERE, not in the
catalog schema: the minimal engine-side validator does not support conditional
'required' (JSON Schema if/then). Clear ValueErrors feed the self-correction loop
(principle 7: perceive and verify; principle 6/8: bounded self-correction).
"""

from __future__ import annotations

from typing import List

# Standard-plane orientation for the sketch. Each entry rotates the sketch's local
# XY plane onto the requested global plane, expressed as (axis, angle_degrees) for
# FreeCAD.Rotation. XY is the identity. The sketch then extrudes along its own
# normal (extrude uses DirMode "Normal"), so these orientations also set the
# extrusion direction.
_PLANE_ROTATIONS = {
    "XY": ((0.0, 0.0, 1.0), 0.0),
    "XZ": ((1.0, 0.0, 0.0), 90.0),
    "YZ": ((0.0, 1.0, 0.0), 90.0),
}


def draw_profile(sketch, shape: str, params: dict,
                 origin=(0.0, 0.0), centered: bool = False) -> None:
    """
    Draw the requested 2D profile into an EXISTING sketch, in its local plane
    (z = 0). Shared by create_sketch (sketch on a standard plane) and
    sketch_on_face (sketch attached to an object's face) so both build identical,
    cleanly closed geometry. `shape` is 'rectangle' or 'circle'; the rectangle
    needs width and height, the circle needs radius (validated here with clear
    ValueErrors, since the stdlib catalog validator cannot do conditional required).

    `origin` (ox, oy) is a reference point in the sketch's local plane. With
    `centered=False` (create_sketch) the rectangle's corner sits at `origin` and a
    circle is centred on `origin` - i.e. origin (0,0) reproduces the historical
    corner-at-origin behaviour. With `centered=True` (sketch_on_face) the profile is
    CENTRED on `origin`, so the feature lands in the middle of the face, not at a
    corner.
    """
    import FreeCAD  # lazy import: available only inside FreeCAD.
    import Part      # Part geometry (LineSegment, Circle) - FreeCAD core.
    import Sketcher  # Sketcher constraints - FreeCAD core.

    ox, oy = float(origin[0]), float(origin[1])

    if shape == "rectangle":
        width = float(params.get("width", 0) or 0)
        height = float(params.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            raise ValueError("a rectangle needs width > 0 and height > 0")
        # Bottom-left corner: at `origin`, or offset so the rectangle is centred.
        x0 = ox - width / 2.0 if centered else ox
        y0 = oy - height / 2.0 if centered else oy
        p0 = FreeCAD.Vector(x0, y0, 0.0)
        p1 = FreeCAD.Vector(x0 + width, y0, 0.0)
        p2 = FreeCAD.Vector(x0 + width, y0 + height, 0.0)
        p3 = FreeCAD.Vector(x0, y0 + height, 0.0)
        sketch.addGeometry(Part.LineSegment(p0, p1), False)
        sketch.addGeometry(Part.LineSegment(p1, p2), False)
        sketch.addGeometry(Part.LineSegment(p2, p3), False)
        sketch.addGeometry(Part.LineSegment(p3, p0), False)
        # Close the loop: end of each segment coincides with the start of the next
        # (point index 1 = start, 2 = end). A clean closed wire extrudes to a solid.
        sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 2, 1, 1))
        sketch.addConstraint(Sketcher.Constraint("Coincident", 1, 2, 2, 1))
        sketch.addConstraint(Sketcher.Constraint("Coincident", 2, 2, 3, 1))
        sketch.addConstraint(Sketcher.Constraint("Coincident", 3, 2, 0, 1))
    elif shape == "circle":
        radius = float(params.get("radius", 0) or 0)
        if radius <= 0:
            raise ValueError("a circle needs radius > 0")
        centre = FreeCAD.Vector(ox, oy, 0.0)
        normal = FreeCAD.Vector(0.0, 0.0, 1.0)  # local plane normal
        sketch.addGeometry(Part.Circle(centre, normal, radius), False)
    elif shape == "polygon":
        _draw_polygon(sketch, params, ox, oy)
    elif shape == "slot":
        _draw_slot(sketch, params, ox, oy)
    elif shape == "polyline":
        _draw_polyline(sketch, params, ox, oy, centered)
    else:
        raise ValueError("shape must be 'rectangle', 'circle', 'polygon', "
                         "'slot' or 'polyline'")


def _close_loop(sketch, n_geoms: int, first_index: int) -> None:
    """Coincidence-constrain n consecutive geometries into a closed loop
    (end of each = start of the next; point index 1 = start, 2 = end)."""
    import Sketcher
    for k in range(n_geoms):
        a = first_index + k
        b = first_index + ((k + 1) % n_geoms)
        sketch.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))


def _draw_polygon(sketch, params: dict, ox: float, oy: float) -> None:
    """A regular polygon with `sides` edges inscribed in circumradius `radius`,
    centred on the origin point (a polygon is inherently centred)."""
    import math
    import FreeCAD
    import Part

    sides = int(params.get("sides", 0) or 0)
    radius = float(params.get("radius", 0) or 0)
    if sides < 3:
        raise ValueError("a polygon needs sides >= 3")
    if radius <= 0:
        raise ValueError("a polygon needs radius > 0 (the circumradius)")
    pts = []
    for k in range(sides):
        a = 2.0 * math.pi * k / sides
        pts.append(FreeCAD.Vector(ox + radius * math.cos(a),
                                  oy + radius * math.sin(a), 0.0))
    first = None
    for k in range(sides):
        idx = sketch.addGeometry(
            Part.LineSegment(pts[k], pts[(k + 1) % sides]), False)
        if first is None:
            first = idx
    _close_loop(sketch, sides, first)


def _draw_slot(sketch, params: dict, ox: float, oy: float) -> None:
    """A rounded slot (asola) centred on the origin point: overall `length`
    along local X, `width` across; two straight edges + two semicircle arcs."""
    import math
    import FreeCAD
    import Part

    length = float(params.get("length", 0) or 0)
    width = float(params.get("width", 0) or 0)
    if length <= 0 or width <= 0:
        raise ValueError("a slot needs length > 0 and width > 0")
    if length <= width:
        raise ValueError("a slot needs length > width (equal would be a "
                         "circle - use shape 'circle' instead)")
    r = width / 2.0
    half = (length - width) / 2.0      # distance from centre to each arc centre
    c_left = FreeCAD.Vector(ox - half, oy, 0.0)
    c_right = FreeCAD.Vector(ox + half, oy, 0.0)
    normal = FreeCAD.Vector(0.0, 0.0, 1.0)
    top_l = FreeCAD.Vector(ox - half, oy + r, 0.0)
    top_r = FreeCAD.Vector(ox + half, oy + r, 0.0)
    bot_l = FreeCAD.Vector(ox - half, oy - r, 0.0)
    bot_r = FreeCAD.Vector(ox + half, oy - r, 0.0)
    # Order: top line (L->R), right arc (top->bottom, sweeping right), bottom
    # line (R->L), left arc (bottom->top, sweeping left) = one closed loop.
    i0 = sketch.addGeometry(Part.LineSegment(top_l, top_r), False)
    sketch.addGeometry(Part.ArcOfCircle(
        Part.Circle(c_right, normal, r), -math.pi / 2.0, math.pi / 2.0), False)
    sketch.addGeometry(Part.LineSegment(bot_r, bot_l), False)
    sketch.addGeometry(Part.ArcOfCircle(
        Part.Circle(c_left, normal, r), math.pi / 2.0, 3.0 * math.pi / 2.0),
        False)
    # NOTE on endpoints: ArcOfCircle(angle1, angle2) runs counter-clockwise, so
    # the right arc starts at the BOTTOM point and ends at the TOP one, and the
    # left arc starts at the TOP and ends at the BOTTOM. Constrain accordingly
    # (geometry indices: i0=top line, i0+1=right arc, i0+2=bottom line,
    # i0+3=left arc; point 1=start, 2=end).
    import Sketcher
    sketch.addConstraint(Sketcher.Constraint("Coincident", i0, 2, i0 + 1, 2))
    sketch.addConstraint(Sketcher.Constraint("Coincident", i0 + 1, 1, i0 + 2, 1))
    sketch.addConstraint(Sketcher.Constraint("Coincident", i0 + 2, 2, i0 + 3, 2))
    sketch.addConstraint(Sketcher.Constraint("Coincident", i0 + 3, 1, i0, 1))


def _draw_polyline(sketch, params: dict, ox: float, oy: float,
                   centered: bool) -> None:
    """
    A CLOSED wire through `points`: [[x, y], [x, y, sagitta], ...] in mm.
    The wire closes automatically (last point back to the first). A third
    number on a point turns the edge FROM that point TO the next into an arc
    bulging by `sagitta` mm (positive = to the left of the travel direction).
    """
    import FreeCAD
    import Part

    raw = params.get("points")
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError("a polyline needs 'points': a list of at least 3 "
                         "[x, y] pairs (a third number on a point makes the "
                         "next edge an arc with that bulge height)")
    pts = []
    sagittas = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ValueError(f"polyline point #{i} must be [x, y] "
                             "(optionally [x, y, bulge])")
        pts.append((float(entry[0]), float(entry[1])))
        sagittas.append(float(entry[2]) if len(entry) >= 3 else 0.0)
    if centered:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        pts = [(p[0] - cx + ox, p[1] - cy + oy) for p in pts]
    else:
        pts = [(p[0] + ox, p[1] + oy) for p in pts]

    n = len(pts)
    first = None
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % n]
        if abs(x2 - x1) < 1e-9 and abs(y2 - y1) < 1e-9:
            raise ValueError(f"polyline points #{k} and #{(k + 1) % n} "
                             "coincide; remove the duplicate")
        s = sagittas[k]
        if abs(s) < 1e-9:
            geo = Part.LineSegment(FreeCAD.Vector(x1, y1, 0.0),
                                   FreeCAD.Vector(x2, y2, 0.0))
        else:
            geo = _arc_from_sagitta(x1, y1, x2, y2, s)
        idx = sketch.addGeometry(geo, False)
        if first is None:
            first = idx
    # Geometric closure is exact by construction; skip per-pair constraints
    # (arc endpoint parameter order varies with the sweep direction).


def _arc_from_sagitta(x1, y1, x2, y2, s):
    """
    Build a Part.ArcOfCircle from a chord (x1,y1)->(x2,y2) and a sagitta `s`
    (bulge height, positive = to the LEFT of the travel direction). Pure
    chord+sagitta geometry: R = (h^2 + s^2) / (2|s|), centre on the chord's
    perpendicular bisector, on the opposite side of the bulge.
    """
    import math
    import FreeCAD
    import Part

    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    h = chord / 2.0
    if abs(s) >= chord * 10.0:
        raise ValueError("polyline arc bulge is too large for its edge")
    R = (h * h + s * s) / (2.0 * abs(s))
    # Unit vector to the LEFT of travel.
    ux, uy = -dy / chord, dx / chord
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    sign = 1.0 if s > 0 else -1.0
    d = R - abs(s)                      # centre-to-chord distance
    cx = mx - sign * ux * d
    cy = my - sign * uy * d
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    apex_a = math.atan2(my + sign * uy * abs(s) - cy,
                        mx + sign * ux * abs(s) - cx)

    def _ccw_contains(start, end, mid):
        span = (end - start) % (2.0 * math.pi)
        rel = (mid - start) % (2.0 * math.pi)
        return rel <= span + 1e-9

    circle = Part.Circle(FreeCAD.Vector(cx, cy, 0.0),
                         FreeCAD.Vector(0.0, 0.0, 1.0), R)
    # ArcOfCircle sweeps CCW from angle1 to angle2: pick the order that passes
    # through the apex, so the arc bulges to the requested side.
    if _ccw_contains(a1, a2, apex_a):
        return Part.ArcOfCircle(circle, a1, a2)
    return Part.ArcOfCircle(circle, a2, a1)


SHAPES = ("rectangle", "circle", "polygon", "slot", "polyline")


def create_sketch(doc, params: dict) -> List:
    """Create a Sketcher sketch (any supported profile) on a standard plane."""
    import FreeCAD  # lazy import: available only inside FreeCAD.

    shape = str(params.get("shape", "")).strip().lower()
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of: {', '.join(SHAPES)}")

    plane = str(params.get("plane", "XY")).strip().upper()
    if plane not in _PLANE_ROTATIONS:
        raise ValueError("plane must be one of: XY, XZ, YZ")

    sketch = doc.addObject("Sketcher::SketchObject", "Sketch")

    # Orient the sketch onto the chosen plane, then offset by the optional origin.
    axis, angle = _PLANE_ROTATIONS[plane]
    rotation = FreeCAD.Rotation(FreeCAD.Vector(*axis), angle)
    origin = FreeCAD.Vector(0.0, 0.0, 0.0)
    placement = params.get("placement")
    if placement and len(placement) >= 3:
        origin = FreeCAD.Vector(float(placement[0]), float(placement[1]),
                                float(placement[2]))
    sketch.Placement = FreeCAD.Placement(origin, rotation)

    draw_profile(sketch, shape, params)
    return [sketch]
