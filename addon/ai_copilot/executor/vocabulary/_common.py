"""
executor/vocabulary/_common.py - small helpers shared by the vocabulary commands.

Phase 2 commands often reference EXISTING objects (a body to drill, the two bodies
of a boolean, the edges to fillet). These helpers resolve those references and
parse sub-element ids ("Edge7" -> 7) in one place, so every command behaves the
same and fails with clear, user-facing messages (principle 7: verify the input).
"""

from __future__ import annotations

from typing import List


def bounding_box(obj):
    """
    Best-effort bounding box of an object's shape, or None if it has none yet.
    Used by commands that must locate an existing body in space (e.g. drilling a
    hole on its top face) instead of trusting coordinates the model guessed
    (principle 7: the agent perceives and verifies).
    """
    shape = getattr(obj, "Shape", None)
    if shape is None:
        return None
    return getattr(shape, "BoundBox", None)


def apply_placement(obj, placement) -> None:
    """
    Apply an optional [x,y,z] origin from a command's `placement` parameter.
    Shared by the create_* primitives (Phase 7 additions use it; the older
    primitives keep their inline version to avoid touching validated code).
    No-op when placement is missing or too short.
    """
    import FreeCAD
    if placement and len(placement) >= 3:
        x, y, z = (float(placement[0]), float(placement[1]), float(placement[2]))
        obj.Placement = FreeCAD.Placement(FreeCAD.Vector(x, y, z),
                                          FreeCAD.Rotation())


def hide_object(obj) -> None:
    """
    Hide an object in the 3D view (best-effort, no-op without a GUI).

    When a feature CONSUMES a base object (fillet/chamfer round the base; a
    boolean fuses/cuts its operands), FreeCAD's interactive commands hide the
    original so you only see the result. Creating the same feature through the
    data API does NOT hide it, which leaves the original body drawn ON TOP of the
    result - e.g. a sharp-edged box covering the rounded fillet, so the rounding
    "looks like it did nothing". We replicate the GUI behaviour explicitly.

    Booleans (Part::Cut/Fuse/Common) already auto-hide their operands via their
    own view provider; this helper covers the features that do not (fillet,
    chamfer). It is defensive: ViewObject is None in headless/console runs.
    """
    try:
        view = getattr(obj, "ViewObject", None)
        if view is not None:
            view.Visibility = False
    except Exception:
        pass


# --- model-friendly axis/plane resolution (ADR 0011) -------------------------
# Phase 6 commands point along an AXIS (rotate) or across a PLANE (mirror). A
# small local model produces free 3D vectors unreliably, so the CONTRACT uses
# short strings ("X"/"Y"/"Z", "XY"/"XZ"/"YZ") and the EXECUTOR maps them to real
# vectors here - the same lesson as ADR 0006/0010 (resolve fragile references in
# the executor, which can read real geometry, not in the model).

_AXIS_VECTORS = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
_PLANE_NORMALS = {"XY": (0.0, 0.0, 1.0), "XZ": (0.0, 1.0, 0.0), "YZ": (1.0, 0.0, 0.0)}


def resolve_axis(axis, default: str = "Z"):
    """
    Map an axis spec to a FreeCAD.Vector. Accepts the strings 'X'/'Y'/'Z' (the
    model-friendly contract) and, for power users, an explicit [x,y,z] vector.
    None falls back to `default`. Raises ValueError on an unknown string.
    """
    import FreeCAD
    if axis is None:
        axis = default
    if isinstance(axis, str):
        key = axis.strip().upper()
        if key not in _AXIS_VECTORS:
            raise ValueError(
                f"unknown axis '{axis}'. Use one of: {', '.join(_AXIS_VECTORS)}")
        return FreeCAD.Vector(*_AXIS_VECTORS[key])
    if isinstance(axis, (list, tuple)) and len(axis) >= 3:
        return FreeCAD.Vector(float(axis[0]), float(axis[1]), float(axis[2]))
    raise ValueError(f"invalid axis: {axis!r} (use 'X'/'Y'/'Z' or [x,y,z])")


def resolve_plane_normal(plane, default: str = "XY"):
    """
    Map a plane spec ('XY'/'XZ'/'YZ') to the FreeCAD.Vector of its NORMAL. Also
    accepts an explicit [x,y,z] normal. None falls back to `default`.
    """
    import FreeCAD
    if plane is None:
        plane = default
    if isinstance(plane, str):
        key = plane.strip().upper()
        if key not in _PLANE_NORMALS:
            raise ValueError(
                f"unknown plane '{plane}'. Use one of: {', '.join(_PLANE_NORMALS)}")
        return FreeCAD.Vector(*_PLANE_NORMALS[key])
    if isinstance(plane, (list, tuple)) and len(plane) >= 3:
        return FreeCAD.Vector(float(plane[0]), float(plane[1]), float(plane[2]))
    raise ValueError(f"invalid plane: {plane!r} (use 'XY'/'XZ'/'YZ' or [x,y,z])")


def bbox_center(obj):
    """
    Centre of the object's bounding box as a FreeCAD.Vector, or the origin if the
    object has no shape yet. Used as the default pivot for rotate (the agent
    perceives the real centre instead of trusting a coordinate the model guessed).
    """
    import FreeCAD
    bb = bounding_box(obj)
    if bb is None:
        return FreeCAD.Vector(0.0, 0.0, 0.0)
    return FreeCAD.Vector((bb.XMin + bb.XMax) / 2.0,
                          (bb.YMin + bb.YMax) / 2.0,
                          (bb.ZMin + bb.ZMax) / 2.0)


def resolve_object(doc, obj_id: str):
    """
    Return the FreeCAD object whose internal Name is `obj_id`.
    Raises ValueError with a helpful message if it does not exist.
    """
    if not obj_id or not isinstance(obj_id, str):
        raise ValueError("an object id (string) is required")
    obj = doc.getObject(obj_id)
    if obj is None:
        # Be forgiving: the model might pass the user-visible Label instead.
        for candidate in doc.Objects:
            if getattr(candidate, "Label", None) == obj_id:
                return candidate
        known = ", ".join(o.Name for o in doc.Objects) or "(document is empty)"
        raise ValueError(f"object '{obj_id}' not found. Existing objects: {known}")
    return obj


def parse_edge_index(edge_ref: str) -> int:
    """
    Convert an edge reference like 'Edge7' (or plain '7') into the 1-based index
    FreeCAD expects in Chamfer/Fillet. Raises ValueError on garbage input.
    """
    if isinstance(edge_ref, int):
        return edge_ref
    s = str(edge_ref).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        raise ValueError(f"invalid edge reference: {edge_ref!r} (expected e.g. 'Edge7')")
    return int(digits)


def parse_edge_indices(edges: List[str]) -> List[int]:
    """Map a list of edge references to their 1-based indices."""
    if not edges:
        raise ValueError("at least one edge id is required")
    return [parse_edge_index(e) for e in edges]


# --- executor-side FACE selection (Phase 7, shell) ----------------------------
# Same philosophy as the edge selector below (ADR 0006) and sketch_on_face
# (ADR 0014): the model says WHERE ('top'/'bottom'), the executor reads the real
# geometry and finds the face - never an index guessed by the model
# (principle 7). sketch_on_face keeps its historical local resolver (validated
# in real FreeCAD); new commands use this shared one.

def select_face_ref(target, where: str = "top", explicit=None) -> str:
    """
    Return a face reference like 'Face3' on `target`: the explicit id if given,
    otherwise the planar face whose normal points along +Z ('top', highest) or
    -Z ('bottom', lowest). Raises ValueError with a clear message otherwise.
    """
    if explicit:
        return str(explicit)
    where = str(where or "top").strip().lower()
    if where not in ("top", "bottom"):
        raise ValueError("'where' must be 'top' or 'bottom' (or pass an "
                         "explicit 'face' id)")
    shape = getattr(target, "Shape", None)
    if shape is None:
        raise ValueError(f"target '{getattr(target, 'Name', '?')}' has no "
                         "shape yet; recompute the document first")
    faces = list(getattr(shape, "Faces", []) or [])
    if not faces:
        raise ValueError(f"target '{getattr(target, 'Name', '?')}' has no faces")
    best_idx = None
    best_z = None
    for i, f in enumerate(faces, start=1):
        try:
            n = f.normalAt(0, 0)
        except Exception:
            continue
        bb = getattr(f, "BoundBox", None)
        if where == "top" and n.z > 0.9:
            z = bb.ZMax if bb is not None else 0.0
            if best_z is None or z > best_z:
                best_z, best_idx = z, i
        elif where == "bottom" and n.z < -0.9:
            z = bb.ZMin if bb is not None else 0.0
            if best_z is None or z < best_z:
                best_z, best_idx = z, i
    if best_idx is None:
        raise ValueError(f"could not find a flat '{where}' face on "
                         f"'{getattr(target, 'Name', '?')}'; pass an explicit "
                         "'face' id")
    return f"Face{best_idx}"


# --- executor-side edge selection (ADR 0006) ---------------------------------
# Fillet/chamfer can pick edges WITHOUT the model enumerating Edge1..EdgeN. The
# executor runs inside FreeCAD, so it can read the real geometry and choose the
# right edges itself (principle 7: the agent perceives and verifies, it does not
# ask a small model to list a dozen edge ids correctly).

EDGE_SELECTORS = ("all", "top", "bottom", "vertical", "horizontal")


class _SimpleBBox:
    """A tiny stand-in for FreeCAD's BoundBox, built from an edge's vertices."""
    __slots__ = ("XMin", "XMax", "YMin", "YMax", "ZMin", "ZMax")

    def __init__(self, xmin, xmax, ymin, ymax, zmin, zmax):
        self.XMin, self.XMax = xmin, xmax
        self.YMin, self.YMax = ymin, ymax
        self.ZMin, self.ZMax = zmin, zmax


def _edge_bbox(edge):
    """
    Best-effort axis-aligned bounding box of a single edge. Prefers the edge's
    own BoundBox (real FreeCAD); falls back to the span of its vertices. Returns
    None if no geometry is available.
    """
    bb = getattr(edge, "BoundBox", None)
    if bb is not None:
        return bb
    verts = getattr(edge, "Vertexes", None)
    if not verts:
        return None
    xs, ys, zs = [], [], []
    for v in verts:
        p = getattr(v, "Point", None)
        if p is None:
            return None
        xs.append(p.x)
        ys.append(p.y)
        zs.append(p.z)
    if not xs:
        return None
    return _SimpleBBox(min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def select_edge_indices(shape, where: str = "all") -> List[int]:
    """
    Return the 1-based indices of `shape`'s edges that match a selector:

      all        -> every edge
      top        -> edges lying flat at the highest Z
      bottom     -> edges lying flat at the lowest Z
      vertical   -> edges running along the Z axis
      horizontal -> edges lying flat in any constant-Z plane (top + bottom)

    Selection is GEOMETRIC (reads each edge's bounding box), assuming the common
    orientation where "up" is +Z. If global geometry is unavailable we fall back
    to "all" so the operation still has edges to act on rather than failing.
    Raises ValueError for an empty shape, an unknown selector, or a selector that
    matches no edge (so the caller can report a clear message and self-correct).
    """
    edges = list(getattr(shape, "Edges", []) or [])
    n = len(edges)
    if n == 0:
        raise ValueError("the target has no edges to operate on")

    where = (where or "all").lower()
    if where not in EDGE_SELECTORS:
        raise ValueError(
            f"unknown edge selector '{where}'. Use one of: {', '.join(EDGE_SELECTORS)}")

    if where == "all":
        return list(range(1, n + 1))

    sbb = getattr(shape, "BoundBox", None)
    if sbb is None:
        return list(range(1, n + 1))  # no global frame: act on all edges.

    z_min, z_max = sbb.ZMin, sbb.ZMax
    span = max(sbb.XMax - sbb.XMin, sbb.YMax - sbb.YMin, z_max - z_min, 1.0)
    tol = span * 1e-4  # generous tolerance, robust to rounding.

    chosen: List[int] = []
    for i, edge in enumerate(edges, start=1):
        ebb = _edge_bbox(edge)
        if ebb is None:
            continue
        ez = abs(ebb.ZMax - ebb.ZMin)
        ex = abs(ebb.XMax - ebb.XMin)
        ey = abs(ebb.YMax - ebb.YMin)
        is_flat = ez <= tol                       # edge stays in one Z plane
        is_vertical = (ez > tol) and (ex <= tol) and (ey <= tol)
        if where == "vertical" and is_vertical:
            chosen.append(i)
        elif where == "horizontal" and is_flat:
            chosen.append(i)
        elif where == "top" and is_flat and abs(ebb.ZMax - z_max) <= tol:
            chosen.append(i)
        elif where == "bottom" and is_flat and abs(ebb.ZMin - z_min) <= tol:
            chosen.append(i)

    if not chosen:
        raise ValueError(
            f"no edges matched selector '{where}' on this shape; "
            "try 'all' or list explicit edge ids")
    return chosen


# --- placement-by-reference helpers (phase 3: placement & assembly) ------------
# Assembly commands place a PART RELATIVE TO ANOTHER PART by reading real
# geometry (a face's centre + outward normal) instead of trusting coordinates
# the model guesses (principle 7: the executor perceives; the model declares
# intent). These helpers turn a *named face* ("top") into a real point and
# normal, measure a part's extent along a direction, compute the rotation that
# aligns one face normal onto another, and perform the mating placement shared
# by place_on (stacking) and mate (general joint).

# The face of the mating part that touches `face` (opposite side). Used by
# place_on to derive the target's touching face from below's face.
PLACE_OPPOSITE = {
    "top": "bottom", "bottom": "top",
    "left": "right", "right": "left",
    "front": "back", "back": "front",
}

_FACES = {
    "top": (lambda bb: (0.5 * (bb.XMin + bb.XMax), 0.5 * (bb.YMin + bb.YMax), bb.ZMax),
             (0, 0, 1)),
    "bottom": (lambda bb: (0.5 * (bb.XMin + bb.XMax), 0.5 * (bb.YMin + bb.YMax), bb.ZMin),
               (0, 0, -1)),
    "right": (lambda bb: (bb.XMax, 0.5 * (bb.YMin + bb.YMax), 0.5 * (bb.ZMin + bb.ZMax)),
              (1, 0, 0)),
    "left": (lambda bb: (bb.XMin, 0.5 * (bb.YMin + bb.YMax), 0.5 * (bb.ZMin + bb.ZMax)),
             (-1, 0, 0)),
    "front": (lambda bb: (0.5 * (bb.XMin + bb.XMax), bb.YMax, 0.5 * (bb.ZMin + bb.ZMax)),
              (0, 1, 0)),
    "back": (lambda bb: (0.5 * (bb.XMin + bb.XMax), bb.YMin, 0.5 * (bb.ZMin + bb.ZMax)),
             (0, -1, 0)),
}


def face_plane(obj, face) -> "tuple":
    """
    Return (center, normal) for a named planar face of obj, derived from its
    bounding box (the object's real extent in space). `face` is one of
    top/bottom/left/right/front/back; the normal points OUTWARD from that face.
    Used by placement-by-reference (place_on/mate): the engine locates the face,
    so the model never guesses a face number or an absolute point.
    """
    import FreeCAD
    bb = bounding_box(obj)
    if bb is None:
        raise ValueError(f"{getattr(obj, 'Name', '?')} has no shape/bbox yet; "
                         "recompute the document first")
    key = str(face).strip().lower()
    if key not in _FACES:
        raise ValueError(
            f"face must be one of top/bottom/left/right/front/back (got {face!r})")
    center_func, normal = _FACES[key]
    center = center_func(bb)
    return FreeCAD.Vector(*center), FreeCAD.Vector(*normal)


def rotation_between(u, v):
    """
    FreeCAD.Rotation that maps unit vector u onto unit vector v, computed from the
    axis (u x v) and angle (acos(u . v)). Used by assembly joints to orient one
    part's face normal onto another's, so the engine computes the real rotation
    instead of the model guessing Euler angles.
    """
    import math
    import FreeCAD

    def _len(x, y, z):
        return math.sqrt(x * x + y * y + z * z)

    ulen = _len(u.x, u.y, u.z) or 1.0
    vlen = _len(v.x, v.y, v.z) or 1.0
    ux, uy, uz = u.x / ulen, u.y / ulen, u.z / ulen
    vx, vy, vz = v.x / vlen, v.y / vlen, v.z / vlen

    dot = ux * vx + uy * vy + uz * vz
    if dot > 0.999999:
        return FreeCAD.Rotation()                    # already parallel
    if dot < -0.999999:
        # Opposite directions: 180 about any axis perpendicular to u.
        ax = 0.0 if abs(ux) > 0.9 else 1.0
        ay = 1.0 if abs(ux) <= 0.9 else 0.0
        az = 0.0
        return FreeCAD.Rotation(FreeCAD.Vector(ax, ay, az), 180.0)
    cx = uy * vz - uz * vy
    cy = uz * vx - ux * vz
    cz = ux * vy - uy * vx
    clen = _len(cx, cy, cz) or 1.0
    angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
    return FreeCAD.Rotation(FreeCAD.Vector(cx / clen, cy / clen, cz / clen), angle)


def place_against(a, a_face, b, b_face, gap: float = 0.0, align: bool = False):
    """
    Position part `a` so its face `a_face` mates part `b`'s face `b_face`: a's
    face touches b's face, `gap` apart, and (when align) a's face normal is turned
    to point away from b. Pure placement: only a.Placement.Base (and, if align,
    its Rotation) changes; a's shape is untouched (principle 2). Shared by place_on
    (stacking, align=False) and mate (general joint, align=True).
    """
    import FreeCAD
    gap = float(gap or 0.0)
    if gap < 0:
        raise ValueError("gap must be >= 0 (negative means overlapping)")
    center_b, n_b = face_plane(b, b_face)
    center_a, n_a = face_plane(a, a_face)
    neg_nb = FreeCAD.Vector(-n_b.x, -n_b.y, -n_b.z)
    rotation = rotation_between(n_a, neg_nb) if align else FreeCAD.Rotation()
    desired_face = center_b + neg_nb * gap
    # The part's shape is defined in LOCAL coords (bbox corner at the local
    # origin), so the placement Base is the point whose local image is `center_a`
    # mapped onto desired_face: Base = desired_face - Rotation.multVec(center_a).
    # This makes a's face TOUCH b's face exactly. The earlier formula placed
    # Base at the part's CENTRE instead of its local origin, leaving a hole of
    # half the part's depth (a silent wrong placement - principle 7).
    a.Placement = FreeCAD.Placement(desired_face - rotation.multVec(center_a),
                                    rotation * a.Placement.Rotation)
    return a
