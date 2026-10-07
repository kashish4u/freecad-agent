#!/usr/bin/env python3
"""
test_integration.py - END-TO-END: small model -> perception -> plan -> execute -> verify.

Phase 6 (headless integration harness). This drives the WHOLE pipeline against the
mock-`FreeCAD` and proves the engine's central guarantee (ACTION_PLAN principle 4):

    "A mis-plan must fail loudly and recover, never silently produce nothing."

It wires the REAL pieces together, exactly as the bridge server does:
    perception.overview(doc)   (Phase 4)  ->  brain.plan(...)    (Phase 5)
        -> executor.execute(...) (Phase 1)  ->  geometry

and runs the plan through a bounded self-repair loop that mirrors
bridge_server._execute_actions: on a failing command it calls brain.repair(...) and
retries; if the model cannot repair within `max_repair` attempts it RAISES loudly
(it never returns a document with nothing drawn).

Why it runs headlessly: the executor imports FreeCAD LAZILY (inside execute), so
installing the mock (tests/mock_freecad.py) makes the entire pipeline runnable with
no real FreeCAD. The mock's recompute() clears State (geometry is always "ok"), so
an execution failure here is driven by a command that PASSES schema validation but
FAILS at runtime - e.g. place_on a non-existent target (place_on raises). Such a
command is kept in plan["valid_actions"] (schema is satisfied) yet returns
ok:False from execute(), exercising the repair loop end to end.

Run: python -m pytest tests/test_integration.py -q
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import mock_freecad  # noqa: E402  (installed inside _doc)

import brain as brain_mod  # noqa: E402
import fake_brain          # noqa: E402
from ai_copilot import executor as _EXEC  # noqa: E402  (FreeCAD imported lazily)
from ai_copilot import perception  # noqa: E402

import pytest  # noqa: E402

CAT = fake_brain.Catalog()


@contextlib.contextmanager
def _doc(name="T"):
    """Run a scenario inside one mock-installed document; auto-cleans up."""
    mock_freecad.install()
    import FreeCAD  # noqa: F401  (mock; newDocument sets it as ActiveDocument)
    try:
        yield FreeCAD.newDocument(name)
    finally:
        mock_freecad.uninstall()


# --------------------------------------------------------------------------- helpers
def _plan_chat(actions):
    """A deterministic fake local model: a chat fn returning a fixed action list."""
    def chat(system, user):
        return {"actions": actions}
    return chat


def _repair_chat(actions):
    """A fake repair model that returns `actions` (asserts the error was fed in)."""
    def chat(system, user):
        assert "FreeCAD error" in user, "repair prompt should include the error"
        return {"actions": actions}
    return chat


def run_plan(brain, doc, request, plan, overview=None, details=None, max_repair=3):
    """Execute a plan's valid_actions with bounded self-repair.

    Mirrors bridge_server._execute_actions: run each validated action; when one
    fails, call brain.repair(...) and retry up to max_repair times. If the model
    cannot repair it (returns None, or keeps failing) it RAISES - never silently
    produce nothing. Returns the id of every object the plan created.
    """
    created = []
    for action in plan.get("valid_actions", []):
        result = _EXEC.execute(action)
        if result.get("ok"):
            created.extend(result.get("created_ids", []))
            continue
        error = result.get("error", "unknown error")
        for _ in range(max_repair):
            repaired = brain.repair(request, action, error, overview, details)
            if repaired is None:
                break
            result = _EXEC.execute(repaired)
            if result.get("ok"):
                created.extend(result.get("created_ids", []))
                break
        if not result.get("ok"):
            raise RuntimeError(
                f"command {action.get('cmd')} failed ({error}) and could not be "
                f"repaired after {max_repair} attempts"
            )
    return created


# ---------------------------------------------------------------------- scenarios
def test_end_to_end_part_never_nothing():
    """A small-model request produces real geometry - not a silent empty doc."""
    with _doc("E2E_Part") as doc:
        overview = perception.overview(doc)
        plan_chat = _plan_chat([
            {"type": "command", "cmd": "create_box",
             "params": {"length": 20, "width": 15, "height": 10}},
        ])
        plan = brain_mod.Brain(catalog=CAT, chat=plan_chat).plan(
            "make a 20x15x10 block", overview=overview)
        created = run_plan(brain_mod.Brain(catalog=CAT, chat=plan_chat),
                           doc, "make a 20x15x10 block", plan, overview)
        assert created, "silently produced nothing!"
        obj = doc.getObject(created[0])
        assert obj is not None and obj.Shape is not None
        bb = obj.Shape.BoundBox
        assert bb.XMax - bb.XMin == pytest.approx(20.0)
        assert bb.YMax - bb.YMin == pytest.approx(15.0)
        assert bb.ZMax - bb.ZMin == pytest.approx(10.0)


def test_end_to_end_assembly_placement():
    """box + cylinder + place_on assembles a part resting on the box top."""
    with _doc("E2E_Assembly") as doc:
        overview = perception.overview(doc)
        actions = [
            {"type": "command", "cmd": "create_box",
             "params": {"length": 20, "width": 15, "height": 6}},
            {"type": "command", "cmd": "create_cylinder",
             "params": {"radius": 3, "height": 8}},
            {"type": "command", "cmd": "place_on",
             "params": {"target": "Cylinder", "below": "Box", "face": "top"}},
        ]
        plan = brain_mod.Brain(catalog=CAT, chat=_plan_chat(actions)).plan(
            "assemble a cap: a cylinder resting on a box", overview=overview)
        created = run_plan(brain_mod.Brain(catalog=CAT, chat=_plan_chat(actions)),
                           doc, "assemble a cap", plan, overview)
        assert len(created) == 3, "assembly produced nothing!"
        cap = doc.getObject("Cylinder")
        # place_on (align=False) sets Base = box top-centre; the cylinder's local
        # bottom face is at z=0, so it rests exactly on the box top (z == box height).
        assert abs(cap.Placement.Base.z - 6.0) < 1e-9
        assert abs(cap.Placement.Base.x - 10.0) < 1e-9
        assert abs(cap.Placement.Base.y - 7.5) < 1e-9


def test_invalid_command_dropped_loudly():
    """A command that fails schema validation is dropped with a note, not run."""
    with _doc("E2E_Dropped") as doc:
        overview = perception.overview(doc)
        actions = [
            {"type": "command", "cmd": "create_box",
             "params": {"length": 20, "width": 15, "height": 10}},
            {"type": "command", "cmd": "create_box", "params": {"length": -1}},  # invalid
            {"type": "command", "cmd": "create_cylinder",
             "params": {"radius": 3, "height": 8}},
        ]
        planner = brain_mod.Brain(catalog=CAT, chat=_plan_chat(actions))
        plan = planner.plan("make a block with a hole", overview=overview)
        # The negative-length box is dropped with a note; only the 2 valid run.
        assert any("dropped" in n.lower() for n in plan.get("notes", [])), plan.get("notes")
        assert len(plan["valid_actions"]) == 2
        created = run_plan(planner, doc, "make a block with a hole", plan, overview)
        assert len(created) == 2
        assert doc.getObject("Box") is not None
        assert doc.getObject("Cylinder") is not None


def test_repair_loop_recovers():
    """A command that passes validation but fails at execution is repaired & retried."""
    with _doc("E2E_Repair") as doc:
        overview = perception.overview(doc)
        # The model misspells the box as "Bass"; place_on passes schema validation
        # (both targets are present strings) but fails at runtime -> triggers repair.
        plan_chat = _plan_chat([
            {"type": "command", "cmd": "create_box",
             "params": {"length": 20, "width": 15, "height": 6}},
            {"type": "command", "cmd": "create_cylinder",
             "params": {"radius": 3, "height": 8}},
            {"type": "command", "cmd": "place_on",
             "params": {"target": "Cylinder", "below": "Bax", "face": "top"}},
        ])
        planner = brain_mod.Brain(catalog=CAT, chat=plan_chat)
        plan = planner.plan("assemble a cap", overview=overview)
        repair_chat = _repair_chat([
            {"type": "command", "cmd": "place_on",
             "params": {"target": "Cylinder", "below": "Box", "face": "top"}},
        ])
        created = run_plan(brain_mod.Brain(catalog=CAT, chat=repair_chat),
                           doc, "assemble a cap", plan, overview)
        assert len(created) == 3, "repair did not recover geometry"
        cap = doc.getObject("Cylinder")
        assert abs(cap.Placement.Base.z - 6.0) < 1e-9


def test_unrepairable_failure_raises():
    """If the model cannot repair, the engine raises loudly (does not go silent)."""
    with _doc("E2E_Loud") as doc:
        overview = perception.overview(doc)
        plan_chat = _plan_chat([
            {"type": "command", "cmd": "create_box",
             "params": {"length": 20, "width": 15, "height": 6}},
            {"type": "command", "cmd": "place_on",
             "params": {"target": "Missing", "below": "Box", "face": "top"}},
        ])
        planner = brain_mod.Brain(catalog=CAT, chat=plan_chat)
        plan = planner.plan("assemble", overview=overview)
        # The repair model keeps returning the SAME bad action -> never recovers.
        # (below="Box" is valid; the only failure is the missing *target*, which the
        # repair cannot fix, so it keeps failing and the engine raises loudly.)
        repair_chat = _repair_chat([
            {"type": "command", "cmd": "place_on",
             "params": {"target": "Missing", "below": "Box", "face": "top"}},
        ])
        brain = brain_mod.Brain(catalog=CAT, chat=repair_chat)
        with pytest.raises(RuntimeError, match="could not be repaired"):
            run_plan(brain, doc, "assemble", plan, overview, max_repair=3)


# -------------------------------------------------------------------------- runner
def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  [ok] {t.__name__}")
    return True


if __name__ == "__main__":
    print("== test_integration: NL -> perception -> plan -> execute -> verify ==")
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"ERROR: {exc}\n{traceback.format_exc()}")
        sys.exit(1)
    print("PASS - the engine draws from a small model, assembles, and "
          "fails loudly instead of silently producing nothing.")
    sys.exit(0)
