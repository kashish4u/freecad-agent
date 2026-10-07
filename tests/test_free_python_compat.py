#!/usr/bin/env python3
"""
test_free_python_compat.py - Phase 1: free-Python geometry compatibility shim.

The model is small and guesses FreeCAD API names that DO NOT exist in this
FreeCAD's `Part` module (Part.makeGeometryFusion, Part.union, Part.cut([...]),
Part.intersect([...]). Rather than trust the model, python_exec.py installs a
shim that resolves every common guess onto the correct Shape method it MEANS.

This test installs a FAKE Part module (no boolean ops, like real FreeCAD before
the shim) and a fake FreeCAD, then:
  - runs the exact spoon-style snippets that used to fail through the REAL
    run_python() and asserts they no longer crash (ok=True),
  - directly verifies the installed shim resolves each guess to the correct
    Shape call (fuse/cut/common),
  - asserts a real Part.makeBox is never shadowed,
  - asserts the hasattr guard does NOT overwrite a name that already exists,
  - asserts a genuinely wrong API still fails loudly (not silently swallowed).

Runnable:
    python tests/test_free_python_compat.py
    pytest tests/test_free_python_compat.py
"""

import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "addon"))


class FakeShape:
    """A shape that records the boolean ops applied to it (deterministic)."""

    def __init__(self, tag: str):
        self.tag = tag

    def fuse(self, other: "FakeShape") -> "FakeShape":
        return FakeShape(f"{self.tag}+{other.tag}")

    def cut(self, other: "FakeShape") -> "FakeShape":
        return FakeShape(f"{self.tag}-{other.tag}")

    def common(self, other: "FakeShape") -> "FakeShape":
        return FakeShape(f"{self.tag}&{other.tag}")


class _FakeDoc:
    def __init__(self):
        self.Objects = []
        self.Name = "T"

    def recompute(self):
        return 0

    def openTransaction(self, label):
        return None

    def commitTransaction(self):
        return None

    def abortTransaction(self):
        return None


def _install_fakes():
    """Register a fake Part (no boolean ops) + fake FreeCAD in sys.modules."""
    fake_part = types.ModuleType("Part")
    fake_part.makeBox = lambda L, W, H: FakeShape("box")

    fake_free = types.ModuleType("FreeCAD")
    fake_free.ActiveDocument = _FakeDoc()
    fake_free.newDocument = lambda name: _FakeDoc()

    saved = {n: sys.modules.get(n) for n in ("Part", "FreeCAD", "FreeCADGui")}
    sys.modules["Part"] = fake_part
    sys.modules["FreeCAD"] = fake_free
    sys.modules.pop("FreeCADGui", None)
    return fake_part, saved


def _run(code):
    from ai_copilot.executor import python_exec
    return python_exec.run_python(code, reason="test")


def run_scenario():
    fake_part, saved = _install_fakes()
    try:
        # The guessed boolean names must be MISSING before the shim installs.
        for name in ("makeGeometryFusion", "GeometryFusion", "union",
                     "cut", "intersect", "common", "intersection", "fuse",
                     "difference", "makeFusion"):
            assert not hasattr(fake_part, name), f"{name} pre-existed"

        # 1) The exact spoon-style snippets that used to crash now run ok=True
        #    (previously: AttributeError: module 'Part' has no attribute
        #    'makeGeometryFusion' / 'union' / 'GeometryFusion').
        for code in (
            "h = Part.makeBox(1, 1, 1); w = Part.makeBox(1, 1, 1); "
            "Part.makeGeometryFusion([h, w])",
            "h = Part.makeBox(1, 1, 1); w = Part.makeBox(1, 1, 1); "
            "Part.GeometryFusion([h, w])",
            "a = Part.makeBox(1, 1, 1); b = Part.makeBox(1, 1, 1); "
            "Part.union(a, b)",
            "a = Part.makeBox(1, 1, 1); b = Part.makeBox(1, 1, 1); "
            "Part.cut([a, b])",
            "a = Part.makeBox(1, 1, 1); b = Part.makeBox(1, 1, 1); "
            "Part.intersect([a, b])",
        ):
            assert _run(code)["ok"], f"snippet failed: {code}"

        # 2) Directly verify the installed shim resolves each guess to the
        #    correct Shape call. makeBox is real; the boolean names are shimmed.
        h = fake_part.makeBox(1, 1, 1)
        w = fake_part.makeBox(1, 1, 1)
        assert fake_part.makeGeometryFusion([h, w]).tag == "box+box"
        assert fake_part.GeometryFusion([h, w]).tag == "box+box"
        assert fake_part.union(h, w).tag == "box+box"
        assert fake_part.cut([h, w]).tag == "box-box"
        assert fake_part.intersect([h, w]).tag == "box&box"
        assert fake_part.common([h, w]).tag == "box&box"

        # 3) Real primitive still works and is never shadowed.
        assert fake_part.makeBox(10, 20, 30).tag == "box"

        # 4) The hasattr guard does NOT overwrite an existing real name.
        fake_part.cut = "REAL_FREECAD_CUT"
        _run("x = 1")
        assert fake_part.cut == "REAL_FREECAD_CUT", "shim shadowed an existing name"

        # 5) A genuinely wrong API still fails loudly (not silently swallowed).
        r = _run("z = Part.does_not_exist()")
        assert not r["ok"], r
        assert "AttributeError" in r["error"], r

        return True
    finally:
        for n in ("Part", "FreeCAD", "FreeCADGui"):
            if n in saved and saved[n] is not None:
                sys.modules[n] = saved[n]
            else:
                sys.modules.pop(n, None)


def test_free_python_compat():
    assert run_scenario(), "free-python compat scenario failed"


if __name__ == "__main__":
    print("== test_free_python_compat: geometry compatibility shim ==")
    try:
        run_scenario()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"ERROR: {exc}\n{traceback.format_exc()}")
        sys.exit(1)
    print("PASS - wrong API guesses resolve to correct geometry; real API unshadowed.")
    sys.exit(0)
