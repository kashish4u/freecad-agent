"""
executor/python_exec.py - the "free Python" channel (principle 5).

When the structured vocabulary is not enough, the model may propose raw FreeCAD
Python. This module runs that code, but always:
  - inside an UNDOABLE transaction, so Ctrl+Z reverts it (principle 6 - safety =
    reversibility), and
  - AFTER the panel has shown the code and a transparency banner (principle 5).
    The transparency is handled in the add-on/panel; here we just execute.

The code runs with a small, explicit namespace:
    FreeCAD / App   -> the FreeCAD module
    FreeCADGui / Gui-> the GUI module (if available)
    doc             -> the active document
This keeps the proposed snippets short and predictable.

Self-correction (principle 8): on failure we return a commandResult with ok=false
and the error text, so the engine can feed it back to the model and retry once.
We do NOT try to sandbox Python (FreeCAD's API is too broad for that); the safety
net is the undoable transaction plus full transparency to the user.
"""

from __future__ import annotations

import types
from typing import List

from .transaction import undoable


def _recompute_ok(doc, before_names: set) -> tuple:
    """Recompute and return (ok, created_names). created = objects new since `before`."""
    doc.recompute()
    created = [o for o in doc.Objects if o.Name not in before_names]
    for obj in created:
        state = getattr(obj, "State", []) or []
        if "Invalid" in state or "Error" in state:
            return False, [o.Name for o in created]
    return True, [o.Name for o in created]


def run_python(code: str, reason: str = "") -> dict:
    """
    Execute free FreeCAD Python inside an undoable transaction.
    Returns a commandResult (shared/commands.schema.json#/$defs/commandResult).
    """
    import FreeCAD  # lazy import: available only inside FreeCAD.

    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "transaction_id": "", "error": "no Python code provided"}

    doc = FreeCAD.ActiveDocument or FreeCAD.newDocument("FreeCAD_Agent")
    before = {o.Name for o in doc.Objects}

    # The explicit, minimal namespace the model's snippet runs against.
    env = {"FreeCAD": FreeCAD, "App": FreeCAD, "doc": doc, "__name__": "__agent_python__"}

    # FreeCAD's geometry module is `Part`. A model writing free Python very
    # frequently guesses `import FreeCADPart` (a natural-sounding name) -- that
    # module does not exist, so `exec` dies on the import before doing anything.
    # Alias FreeCADPart -> Part and also expose Part directly, so either spelling
    # works and the model's snippet can run.
    import sys
    try:
        import Part
        env["Part"] = Part
        sys.modules.setdefault("FreeCADPart", Part)
    except Exception:
        pass

    # Geometry compatibility shim (dumb-model / smart-kernel).
    #
    # The model is small and frequently guesses API names that do NOT exist in
    # this FreeCAD's `Part` module -- e.g. Part.makeGeometryFusion,
    # Part.GeometryFusion, Part.union(a, b), Part.cut([...]), Part.intersect([...]).
    # The repair loop feeds each error back, but the model only varies the *wrong*
    # guess. Rather than trust the model to know the exact API, we resolve every
    # common guess onto the correct Shape method it MEANS, so a wrong name still
    # yields the right geometry. Real FreeCAD functions are never shadowed
    # (the hasattr guard installs only missing names).
    if isinstance(Part, types.ModuleType):

        def _fuse(shapes):
            shapes = list(shapes)
            if not shapes:
                raise ValueError("fuse: no shapes")
            out = shapes[0]
            for s in shapes[1:]:
                out = out.fuse(s)
            return out

        def _subtract(shapes):
            shapes = list(shapes)
            if len(shapes) < 2:
                raise ValueError("cut: need at least 2 shapes")
            out = shapes[0]
            for s in shapes[1:]:
                out = out.cut(s)
            return out

        def _common(shapes):
            shapes = list(shapes)
            if len(shapes) < 2:
                raise ValueError("intersect: need at least 2 shapes")
            out = shapes[0]
            for s in shapes[1:]:
                out = out.common(s)
            return out

        for _name, _fn in (
            ("makeGeometryFusion", _fuse),
            ("GeometryFusion", _fuse),
            ("makeFusion", lambda a, b, t=1: _fuse([a, b])),
            ("union", lambda a, b: _fuse([a, b])),
            ("fuse", lambda a, b: _fuse([a, b])),
            ("cut", _subtract),
            ("difference", _subtract),
            ("intersect", _common),
            ("common", _common),
            ("intersection", _common),
        ):
            if not hasattr(Part, _name):
                setattr(Part, _name, _fn)

    try:
        import FreeCADGui  # optional: not present in headless mode
        env["FreeCADGui"] = FreeCADGui
        env["Gui"] = FreeCADGui
    except Exception:
        pass

    try:
        with undoable(doc, "python.execute") as (tx_id, _label):
            exec(compile(code, "<agent-python>", "exec"), env)  # noqa: S102 - by design (principle 5)
            recompute_ok, created = _recompute_ok(doc, before)
            if not recompute_ok:
                raise ValueError("recompute failed: invalid geometry after the script")
        return {
            "ok": True,
            "transaction_id": tx_id,
            "created_ids": created,
            "recompute_ok": recompute_ok,
        }
    except Exception as exc:
        # The transaction was aborted: nothing is left in the document.
        return {"ok": False, "transaction_id": "", "error": f"{type(exc).__name__}: {exc}"}
