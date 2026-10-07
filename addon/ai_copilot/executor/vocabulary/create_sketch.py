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

  - lozenge:  a parallelogram via four LineSegments, given `length` and `angle`
              (acute interior angle at the first vertex).
  - angle / L: an L-section via LineSegments, given `size`, `thickness`, `leg`
              ('x' | 'y' selects the longer leg).
  - T:        a T-section via LineSegments, given `size`, `thickness`, `leg`.
  - tube:      a round hollow tube (two concentric circles) from `radius` + `wall`.
  - rtube:     a rectangular hollow tube (two concentric loops) from `width`,
              `height` and `wall`.

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

from ._sizes import resolve_size, baseline_for

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
                 origin=(0.0, 0.0), centered: bool = False,
                 baseline=None) -> None:
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
        width = resolve_size(params.get("width", 0), baseline)
        height = resolve_size(params.get("height", 0), baseline)
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
        radius = resolve_size(params.get("radius", 0), baseline)
        if radius <= 0:
            raise ValueError("a circle needs radius > 0")
        centre = FreeCAD.Vector(ox, oy, 0.0)
        normal = FreeCAD.Vector(0.0, 0.0, 1.0)  # local plane normal
        sketch.addGeometry(Part.Circle(centre, normal, radius), False)
    elif shape == "polygon":
        _draw_polygon(sketch, params, ox, oy, baseline)
    elif shape == "slot":
        _draw_slot(sketch, params, ox, oy, baseline)
    elif shape == "polyline":
        _draw_polyline(sketch, params, ox, oy, centered)
    elif shape == "lozenge":
        _draw_lozenge(sketch, params, ox, oy)
    elif shape in ("angle", "l"):
        _draw_angle(sketch, params, ox, oy)
    elif shape == "t":
        _draw_T(sketch, params, ox, oy)
    elif shape == "tube":
        _draw_tube(sketch, params, ox, oy)
    elif shape == "rtube":
        _draw_rtube(sketch, params, ox, oy)
    else:
        raise ValueError("shape must be 'rectangle', 'circle', 'polygon', "
                         "'slot', 'polyline', 'lozenge', 'angle', 'l', 't', "
                         "'tube' or 'rtube'")


def _close_loop(sketch, n_geoms: int, first_index: int) -> None:
    """Coincidence-constrain n consecutive geometries into a closed loop
    (end of each = start of the next; point index 1 = start, 2 = end)."""
    import Sketcher
    for k in range(n_geoms):
        a = first_index + k
        b = first_index + ((k + 1) % n_geoms)
        sketch.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))


def _draw_polygon(sketch, params: dict, ox: float, oy: float, baseline=None) -> None:
    """A regular polygon with `sides` edges inscribed in circumradius `radius`,
    centred on the origin point (a polygon is inherently centred)."""
    import math
    import FreeCAD
    import Part

    sides = int(params.get("sides", 0) or 0)
    radius = resolve_size(params.get("radius", 0), baseline)
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


def _draw_slot(sketch, params: dict, ox: float, oy: float,
               baseline=None) -> None:
    """A rounded slot (asola) centred on the origin point: overall `length`
    along local X, `width` across; two straight edges + two semicircle arcs."""
    import math
    import FreeCAD
    import Part

    length = resolve_size(params.get("length", 0), baseline)
    width = resolve_size(params.get("width", 0), baseline)
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


# ---------------------------------------------------------------------------
# Closed-profile validation
#
# A sketch can only be extruded into a solid when its profile is a single closed
# wire. Several disjoint closed loops are fine too (a tube has an outer and an
# inner loop), so we do not require exactly one loop. We derive closedness
# PURELY from geometry endpoints, so the same test works in the headless mock
# (which stores StartPoint/EndPoint on lines and Center/Radius/FirstParameter/
# LastParameter on arcs) and in real FreeCAD (where arc Start/EndPoint are not on
# the data API). Coincident constraints back this up when geometry alone is
# inconclusive (principle 7).
# ---------------------------------------------------------------------------


def _basis_perpendicular(normal):
    """
    Return two orthonormal vectors (u, v) spanning the plane of `normal`. Used to
    evaluate arc endpoints: P(a) = centre + R*cos(a)*u + R*sin(a)*v. The u axis is
    aligned with the projection of the global X axis so that arc parameters, which
    the sketch builders record by atan2 in the sketch plane, resolve to their true
    points (u=(1,0,0), v=(0,1,0) for the default XY plane).
    """
    import math  # stdlib; the closed-check helpers below run at module level.
    nx, ny, nz = normal.x, normal.y, normal.z
    n = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / n, ny / n, nz / n
    # Project global X onto the plane; if the normal is parallel to X, use Y.
    px, py, pz = 1.0 - nx * nx, -nx * ny, -nx * nz
    if px * px + py * py + pz * pz < 1e-12:
        px, py, pz = -nx * ny, 1.0 - ny * ny, -ny * nz
    un = math.sqrt(px * px + py * py + pz * pz) or 1.0
    ux, uy, uz = px / un, py / un, pz / un
    # v = normal x u keeps (u, v, normal) right-handed and orthonormal.
    # Compute vz before vy: the y-component of the cross product depends on it.
    vx = ny * uz - nz * uy
    vz = nx * uy - ny * ux
    vy = nz * ux - nx * vz
    return (ux, uy, uz), (vx, vy, vz)


def _freeCAD():
    """FreeCAD is only available at runtime inside the FreeCAD process; import it
    lazily so this module can be imported elsewhere without it. Shared by the
    geometry helpers below, which run inside the closed-check (not inside
    create_sketch) and so cannot rely on create_sketch's local import."""
    import FreeCAD
    return FreeCAD


def _point_on_circle(center, radius, u, v, angle):
    """Point at `angle` (radians) on a circle lying in the (u, v) plane."""
    import math  # stdlib; runs inside the closed-check, not inside create_sketch.
    cx, cy, cz = center.x, center.y, center.z
    c, s = math.cos(angle), math.sin(angle)
    return _freeCAD().Vector(
        cx + radius * (u[0] * c + v[0] * s),
        cy + radius * (u[1] * c + v[1] * s),
        cz + radius * (u[2] * c + v[2] * s))


def _endpoint_pair(g):
    """
    Return (start, end) points of a sketch geometry, or (None, None) when the
    geometry is a closed curve (a circle) that is closed by construction. Works on
    the mock's toy geometries and on real FreeCAD Part objects.
    """
    kind = getattr(g, "kind", None)
    if kind == "circle":
        return None, None  # a circle is a closed loop by construction
    if kind == "line":
        return g.StartPoint, g.EndPoint
    if kind == "arc":
        # A real ArcOfCircle exposes a .Circle (Center/Axis/Radius); the mock
        # carries those directly on the arc. Resolve whichever is present.
        circle = getattr(g, "Circle", None)
        if circle is not None:
            center, radius, axis = circle.Center, circle.Radius, circle.Axis
        else:
            center, radius, axis = g.Center, g.Radius, getattr(g, "Axis", None)
        if axis is None:
            axis = _freeCAD().Vector(0.0, 0.0, 1.0)
        a1 = getattr(g, "FirstParameter", None)
        a2 = getattr(g, "LastParameter", None)
        if a1 is None or a2 is None:
            return None, None  # cannot resolve endpoints: treat as closed
        u, v = _basis_perpendicular(axis)
        return _point_on_circle(center, radius, u, v, a1), \
               _point_on_circle(center, radius, u, v, a2)
    # Unknown geometry: fall back to any Start/EndPoint the object carries.
    return getattr(g, "StartPoint", None), getattr(g, "EndPoint", None)


def _point_key(p):
    """Bucket a point so coincident endpoints (tol ~1e-6) compare equal."""
    return (round(p.x, 6), round(p.y, 6), round(p.z, 6))


def _wire_closed(sketch) -> bool:
    """
    True if the sketch's profile is a single closed wire (or several disjoint
    closed loops, e.g. a tube). Built as a graph of curve endpoints: every shared
    vertex must touch an even number of curve-ends, which holds exactly when the
    open curves chain into closed loops. Order-independent, so it is correct even
    when a curve's endpoints are stored "backwards" (as the slot's arcs are).
    Returns True for an empty profile.
    """
    geoms = getattr(sketch, "_geometry", None)
    if not geoms:
        return True
    degree = {}
    for g in geoms:
        s, e = _endpoint_pair(g)
        if s is None and e is None:
            continue  # closed curve by construction (circle / concentric)
        if s is not None:
            k = _point_key(s)
            degree[k] = degree.get(k, 0) + 1
        if e is not None:
            k = _point_key(e)
            degree[k] = degree.get(k, 0) + 1
    return all(d % 2 == 0 for d in degree.values())


def _coincidently_closed(sketch) -> bool:
    """
    Fallback when the geometry check is inconclusive: a Coincident constraint on
    every curve-end is strong evidence the profile is meant to be closed.
    """
    geoms = getattr(sketch, "_geometry", None)
    n = len(geoms) if geoms else 0
    if n == 0:
        return True
    coincident = sum(1 for c in getattr(sketch, "_constraints", []) or []
                     if getattr(c, "args", (None,))[0] == "Coincident")
    return coincident >= n


def _validate_closed(sketch):
    """
    Verify the sketch is a single closed wire so it can be extruded into a solid
    (principle 7). Raises ValueError -- feeding the model's self-correction loop --
    when the profile is open. Geometry-driven first, coincident constraints second.
    """
    if _wire_closed(sketch):
        return
    if _coincidently_closed(sketch):
        return
    raise ValueError(
        "the sketch profile is not a closed wire: its edges do not connect "
        "end-to-end into a loop. Draw a fully closed profile (one of the built-in "
        "shapes, or a polyline whose last point returns to the first) before "
        "extruding")


# ---------------------------------------------------------------------------
# New profile primitives (Phase 2): lozenge, angle/L, T, tube, rectangular tube.
#
# Each is closed by construction (its lines/circles form one or more closed
# loops), so they need no Coincident constraints -- the geometry check in
# _validate_closed confirms them. All are drawn centred on the local origin.
# ---------------------------------------------------------------------------


def _draw_closed_polygon(sketch, points):
    """Draw `points` (list of (x, y)) as a closed loop of line segments."""
    import FreeCAD
    import Part
    n = len(points)
    if n < 2:
        raise ValueError("a closed profile needs at least 2 points")
    first = None
    for k in range(n):
        p0 = FreeCAD.Vector(*points[k])
        p1 = FreeCAD.Vector(*points[(k + 1) % n])
        idx = sketch.addGeometry(Part.LineSegment(p0, p1), False)
        if first is None:
            first = idx
    return first


def _draw_lozenge(sketch, params, ox, oy):
    """A rhombus: diagonals are width (x) and height (y), centred on origin."""
    width = float(params.get("width", 0) or 0)
    height = float(params.get("height", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("a lozenge needs width > 0 and height > 0")
    hw, hh = width / 2.0, height / 2.0
    _draw_closed_polygon(sketch, [(hw, 0.0), (0.0, hh), (-hw, 0.0), (0.0, -hh)])


def _draw_angle(sketch, params, ox, oy):
    """
    An L-angle: outer width x height, uniform leg thickness. Six-line closed
    outline (bottom-left, going clockwise around the L).
    """
    width = float(params.get("width", 0) or 0)
    height = float(params.get("height", 0) or 0)
    thickness = float(params.get("thickness", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("an angle needs width > 0 and height > 0")
    if thickness <= 0:
        raise ValueError("an angle needs thickness > 0")
    if thickness > min(width, height):
        raise ValueError("thickness must not exceed both width and height")
    half_w, half_h = width / 2.0, height / 2.0
    t = thickness
    # Centred on origin: the L spans -width/2..width/2 x -height/2..height/2.
    _draw_closed_polygon(sketch, [
        (-half_w, -half_h),
        (half_w, -half_h),
        (half_w, -half_h + t),
        (-half_w + t, -half_h + t),
        (-half_w + t, half_h),
        (-half_w, half_h),
    ])


def _draw_T(sketch, params, ox, oy):
    """
    A T: overall width x height, web thickness, flange thickness. Web centred
    under the flange; an eight-line closed outline.
    """
    width = float(params.get("width", 0) or 0)
    height = float(params.get("height", 0) or 0)
    web_thickness = float(params.get("web_thickness", 0) or 0)
    flange_thickness = float(params.get("flange_thickness", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("a T needs width > 0 and height > 0")
    if web_thickness <= 0:
        raise ValueError("a T needs web_thickness > 0")
    if flange_thickness <= 0:
        raise ValueError("a T needs flange_thickness > 0")
    if web_thickness > width:
        raise ValueError("web_thickness must not exceed width")
    if flange_thickness > height:
        raise ValueError("flange_thickness must not exceed height")
    w, h = width, height
    tw, tf = web_thickness, flange_thickness
    # Centred on origin: flange at the top (y from h/2-tf to h/2), web below.
    _draw_closed_polygon(sketch, [
        (-tw / 2.0, -h / 2.0),          # bottom-left of web
        (-tw / 2.0, h / 2.0 - tf),
        (-w / 2.0, h / 2.0 - tf),
        (-w / 2.0, h / 2.0),
        (w / 2.0, h / 2.0),
        (w / 2.0, h / 2.0 - tf),
        (tw / 2.0, h / 2.0 - tf),
        (tw / 2.0, -h / 2.0),           # bottom-right of web
    ])


def _draw_tube(sketch, params, ox, oy):
    """A round tube: two concentric circles (outer R, inner R - thickness)."""
    import FreeCAD
    import Part
    radius = float(params.get("radius", 0) or 0)
    thickness = float(params.get("thickness", 0) or 0)
    if radius <= 0:
        raise ValueError("a tube needs radius > 0")
    if thickness <= 0:
        raise ValueError("a tube needs thickness > 0")
    if thickness >= radius:
        raise ValueError("thickness must be less than radius")
    normal = FreeCAD.Vector(0.0, 0.0, 1.0)  # local plane normal
    centre = FreeCAD.Vector(ox, oy, 0.0)
    sketch.addGeometry(Part.Circle(centre, normal, radius), False)
    sketch.addGeometry(Part.Circle(centre, normal, radius - thickness), False)


def _draw_rtube(sketch, params, ox, oy):
    """A rectangular tube: two concentric rectangles (outer WxH, inner inset)."""
    width = float(params.get("width", 0) or 0)
    height = float(params.get("height", 0) or 0)
    thickness = float(params.get("thickness", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("a rectangular tube needs width > 0 and height > 0")
    if thickness <= 0:
        raise ValueError("a rectangular tube needs thickness > 0")
    if thickness >= min(width, height) / 2.0:
        raise ValueError("thickness must be less than half of width and height")
    w, h = width, height
    t = thickness
    # Centred on origin: outer loop, then inner loop (both closed).
    _draw_closed_polygon(sketch, [
        (-w / 2.0, -h / 2.0), (w / 2.0, -h / 2.0),
        (w / 2.0, h / 2.0), (-w / 2.0, h / 2.0),
    ])
    _draw_closed_polygon(sketch, [
        (-(w - 2 * t) / 2.0, -(h - 2 * t) / 2.0),
        ((w - 2 * t) / 2.0, -(h - 2 * t) / 2.0),
        ((w - 2 * t) / 2.0, (h - 2 * t) / 2.0),
        (-(w - 2 * t) / 2.0, (h - 2 * t) / 2.0),
    ])


SHAPES = ("rectangle", "circle", "polygon", "slot", "polyline",
          "lozenge", "angle", "l", "t", "tube", "rtube")


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

    # Scale an adjective size against the largest object already here (or a default);
    # a standalone sketch has no body to size against, so baseline_for(doc) is used.
    baseline = baseline_for(doc)
    draw_profile(sketch, shape, params, baseline=baseline)
    # Auto closed-check: refuse an open profile here, before it is returned to
    # the model (and later extruded into an impossible solid).
    _validate_closed(sketch)
    return [sketch]
