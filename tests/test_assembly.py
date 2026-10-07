"""
Test the placement vocabulary: place_on, mate, group, label.

Each test is self-contained: it runs inside one mock-`FreeCAD`-installed document
(via the `_doc` context manager), executes exactly one command, and asserts on the
resulting geometry/properties. No shared state, no ordering dependence.

The mock FreeCAD (tests/mock_freecad.py) is a faithful structural stand-in:
real Vector/Rotation/Placement math, real Object/Document/Group classes,
recompute() synthesizes toy shapes with bounding boxes.

Run: python -m pytest tests/ -q   (mock auto-installed via conftest or here)
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon"))

import mock_freecad  # noqa: E402  (installed on sys.path above)

from ai_copilot import executor as _EXEC  # noqa: E402  (FreeCAD is imported lazily)
from ai_copilot.executor.vocabulary._common import face_plane  # noqa: E402


@contextlib.contextmanager
def _doc(name: str = "T"):
    """Run a scenario inside one mock-installed document; auto-cleans up."""
    mock_freecad.install()
    import FreeCAD  # noqa: F401  (mock, installed above)
    try:
        yield FreeCAD.newDocument(name)
    finally:
        mock_freecad.uninstall()


def _box(doc, w, h, d):
    """Create a w x h x d box; return (object-name, object).

    create_box returns [obj]; execute() wraps it as created_ids, so the id is the
    object's real (auto-uniquified) Name -- usable directly as a command param.
    """
    result = _EXEC.execute(
        {"cmd": "create_box",
         "params": {"length": w, "width": h, "height": d}}
    )
    oid = result["created_ids"][0]
    return oid, doc.getObject(oid)


def _close(a, b, tol=1e-6):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def _world_face(obj, face):
    """
    World-space centre + outward normal of a named face of `obj`, after its
    placement is applied. `face_plane` derives the local centre + normal from the
    object's real bounding box; the placement maps them into world space.
    """
    local_c, local_n = face_plane(obj, face)
    p = obj.Placement
    wc, wn = p.multVec(local_c), p.Rotation.multVec(local_n)
    return (wc.x, wc.y, wc.z), (wn.x, wn.y, wn.z)


def test_place_on_rest_on_top():
    with _doc() as doc:
        _box(doc, 20, 10, 6)
        pid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "place_on",
                           "params": {"target": pid, "below": "Box",
                                      "face": "top", "gap": 0, "align": False}})
        assert r["ok"], r
        bottom, _ = _world_face(part, "bottom")
        assert _close(bottom, (10.0, 5.0, 6.0)), bottom
        assert part.Placement.Base.z == 6.0, part.Placement.Base


def test_place_on_with_gap():
    with _doc() as doc:
        _box(doc, 20, 10, 6)
        pid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "place_on",
                           "params": {"target": pid, "below": "Box",
                                      "face": "top", "gap": 1, "align": False}})
        assert r["ok"], r
        bottom, _ = _world_face(part, "bottom")
        assert _close(bottom, (10.0, 5.0, 5.0)), bottom


def test_place_on_below():
    with _doc() as doc:
        pid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "place_on",
                           "params": {"target": pid, "below": "Box",
                                      "face": "bottom", "gap": 0, "align": False}})
        assert r["ok"], r
        top, _ = _world_face(part, "top")
        assert _close(top, (2.0, 2.0, 0.0)), top


def test_place_on_missing_reference():
    with _doc() as doc:
        pid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "place_on",
                           "params": {"target": pid, "below": "Nope",
                                      "face": "top", "gap": 0, "align": False}})
        assert not r["ok"], r
        assert "not found" in r["error"]


def test_mate_align_flips_normal():
    with _doc() as doc:
        a_pid, a = _box(doc, 4, 4, 2)
        b_pid, b = _box(doc, 6, 6, 6)
        r = _EXEC.execute({"cmd": "mate",
                           "params": {"a": a_pid, "face_a": "top",
                                      "b": b_pid, "face_b": "bottom",
                                      "gap": 0, "align": True}})
        assert r["ok"], r
        a_top_c, a_top_n = _world_face(a, "top")
        assert _close(a_top_n, (0, 0, 1)), a_top_n
        b_bot_c, b_bot_n = _world_face(b, "bottom")
        assert _close(b_bot_n, (0, 0, -1)), b_bot_n


def test_mate_no_align_keeps_orientation():
    with _doc() as doc:
        a_pid, a = _box(doc, 4, 4, 2)
        b_pid, b = _box(doc, 6, 6, 6)
        r = _EXEC.execute({"cmd": "mate",
                           "params": {"a": a_pid, "face_a": "top",
                                      "b": b_pid, "face_b": "bottom",
                                      "gap": 0, "align": False}})
        assert r["ok"], r
        a_top_c, a_top_n = _world_face(a, "top")
        b_bot_c, b_bot_n = _world_face(b, "bottom")
        # aligned = face_a centre at face_b centre (no flip)
        assert _close(a_top_c, b_bot_c), (a_top_c, b_bot_c)
        assert _close(a_top_n, (0, 0, 1)), a_top_n


def test_mate_general_joint_front_to_top():
    with _doc() as doc:
        a_pid, a = _box(doc, 4, 4, 2)
        b_pid, b = _box(doc, 6, 6, 6)
        r = _EXEC.execute({"cmd": "mate",
                           "params": {"a": a_pid, "face_a": "front",
                                      "b": b_pid, "face_b": "top",
                                      "gap": 0, "align": True}})
        assert r["ok"], r
        a_front_c, a_front_n = _world_face(a, "front")
        b_top_c, b_top_n = _world_face(b, "top")
        assert _close(a_front_c, b_top_c), (a_front_c, b_top_c)
        assert _close(a_front_n, (0, 0, -1)), a_front_n  # opposes b's top normal (0,0,1)


def test_mate_missing_reference():
    with _doc() as doc:
        a_pid, a = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "mate",
                           "params": {"a": a_pid, "face_a": "top",
                                      "b": "Nope", "face_b": "bottom",
                                      "gap": 0, "align": True}})
        assert not r["ok"], r
        assert "not found" in r["error"]


def test_mate_bad_face_refused():
    with _doc() as doc:
        a_pid, a = _box(doc, 4, 4, 2)
        b_pid, b = _box(doc, 6, 6, 6)
        r = _EXEC.execute({"cmd": "mate",
                           "params": {"a": a_pid, "face_a": "not_a_face",
                                      "b": b_pid, "face_b": "bottom",
                                      "gap": 0, "align": True}})
        assert not r["ok"], r
        assert "face" in r["error"].lower()


def test_group_create_and_members():
    with _doc() as doc:
        _box(doc, 4, 4, 2)
        _box(doc, 6, 6, 3)
        r = _EXEC.execute({"cmd": "group",
                           "params": {"name": "Assembly",
                                      "members": ["Box", "Box001"]}})
        assert r["ok"], r
        g = doc.getObject("Assembly")
        assert g is not None
        assert [m.Name for m in g.Group] == ["Box", "Box001"]


def test_group_reuse_appends_preserving_order():
    with _doc() as doc:
        _box(doc, 4, 4, 2)
        _box(doc, 6, 6, 3)
        _EXEC.execute({"cmd": "group", "params": {"name": "A", "members": ["Box"]}})
        r = _EXEC.execute({"cmd": "group",
                           "params": {"name": "A", "members": ["Box001"]}})
        assert r["ok"], r
        g = doc.getObject("A")
        assert [m.Name for m in g.Group] == ["Box", "Box001"]


def test_group_missing_member_refused():
    with _doc() as doc:
        _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "group",
                           "params": {"name": "A", "members": ["Box", "Nope"]}})
        assert not r["ok"], r
        assert "not found" in r["error"]


def test_label_name_and_note():
    with _doc() as doc:
        oid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "label",
                           "params": {"target": oid, "label": "M8 x 30 steel",
                                      "note": "Hex bolt"}})
        assert r["ok"], r
        assert part.Label == "M8 x 30 steel"
        assert part.Note == "Hex bolt"


def test_label_name_only():
    with _doc() as doc:
        oid, part = _box(doc, 4, 4, 2)
        r = _EXEC.execute({"cmd": "label",
                           "params": {"target": oid, "label": "Plate"}})
        assert r["ok"], r
        assert part.Label == "Plate"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"ok  {t.__name__}")
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if passed != len(tests):
        raise SystemExit(1)
