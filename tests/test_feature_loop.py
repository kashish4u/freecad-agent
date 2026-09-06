#!/usr/bin/env python3
"""
test_feature_loop.py - the agentic per-feature loop (ADR 0017).

All scenarios run WITHOUT FreeCAD and WITHOUT Ollama, over a real socket pair
(engine Session on one side, a scripted add-on on the other), with a scripted
brain that decomposes and plans deterministically.

Scenarios:
  1. agentic run     - 2 features: per-feature status, one undo group per
                       feature (begin/end committed), run-state summary fed to
                       the second plan, chaining across features.
  2. flat fallback   - decompose() returns [] -> the classic v0.12 flat flow,
                       and NO undo group is opened (per-action undo preserved).
  3. replan+rollback - a feature fails, its group is ABORTED, the feature is
                       replanned once with the failure as feedback, then works.
  4. stop on failure - a feature fails twice -> the RUN stops; the following
                       feature is never planned.
  5. feature bound   - decompose() proposes 12 features -> only MAX_FEATURES run.
  6. degraded mode   - the add-on does NOT know transaction.begin (old add-on):
                       the run still works, per-action undo, note emitted.
  7. cancel mid-run  - cancel while feature 2 is being planned: feature 2 runs
                       NOTHING, feature 1's results remain, result is cancelled.

Runnable:
    python tests/test_feature_loop.py
    pytest tests/test_feature_loop.py
"""

import os
import socket
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "engine"))

from bridge import FramedConnection, JsonRpcPeer, PROTOCOL_VERSION  # noqa: E402
import bridge_server  # noqa: E402
import fake_brain      # noqa: E402

TOKEN = "feature-token"

BOX = {"type": "command", "cmd": "create_box",
       "params": {"length": 100, "width": 60, "height": 10}}
HOLE = {"type": "command", "cmd": "drill_hole",
        "params": {"target": "Box", "diameter": 6, "depth": 10}}
FILLET = {"type": "command", "cmd": "fillet",
          "params": {"target": "Box", "radius": 3, "where": "vertical"}}


class ScriptedBrain:
    """Deterministic brain: canned decomposition + per-feature plans."""

    def __init__(self, features, plans, block_plan_at=None):
        """
        features: list returned by decompose().
        plans:    list of plans; plan() pops the next one on each call. Each
                  entry is a list of actions (valid as they are).
        block_plan_at: 1-based plan() call number that BLOCKS until .release
                  is set (to test cancel during a feature's inference).
        """
        self.features = list(features)
        self.plans = list(plans)
        self.plan_calls = []          # kwargs seen by each plan() call
        self.block_plan_at = block_plan_at
        self.plan_started = threading.Event()
        self.release = threading.Event()

    def availability(self):
        return {"available": True, "model": "fake-model", "models": ["fake-model"]}

    def decompose(self, text, overview=None):
        return list(self.features)

    def plan(self, text, overview=None, details=None,
             feature=None, done=None, feedback=None):
        self.plan_calls.append({"feature": feature,
                                "done": list(done or []),
                                "feedback": feedback})
        if self.block_plan_at == len(self.plan_calls):
            self.plan_started.set()
            self.release.wait(timeout=5)
        actions = self.plans.pop(0) if self.plans else []
        return {"actions": actions, "valid_actions": actions,
                "notes": [], "clarification": None}

    def repair(self, *a, **k):
        return None  # per-action repair is covered by other tests


class FakeAddon:
    """Scripted add-on side: records everything the engine asks for."""

    def __init__(self, peer, fail_cmds=None, support_groups=True):
        self.peer = peer
        self.executed = []       # (cmd, params) in order
        self.tx_events = []      # ("begin", label) / ("end", abort)
        self.statuses = []
        self.fail_cmds = dict(fail_cmds or {})  # cmd -> how many times to fail
        self.objects = []        # ids "existing" in the fake document
        peer.register("perception.overview", lambda p: {
            "document_name": "T", "object_count": len(self.objects),
            "objects": [{"id": o, "type": "Part::Box", "label": o}
                        for o in self.objects]})
        peer.register("perception.detail", lambda p: {})
        peer.register("command.execute", self._execute)
        peer.register("python.execute", self._execute_py)
        peer.register("agent.status", lambda p: self.statuses.append(p))
        if support_groups:
            peer.register("transaction.begin",
                          lambda p: self.tx_events.append(("begin", p.get("label")))
                          or {"ok": True})
            peer.register("transaction.end",
                          lambda p: self.tx_events.append(("end", bool(p.get("abort"))))
                          or {"ok": True})

    def _execute(self, params):
        cmd = params.get("cmd")
        self.executed.append((cmd, params.get("params")))
        left = self.fail_cmds.get(cmd, 0)
        if left > 0:
            self.fail_cmds[cmd] = left - 1
            return {"ok": False, "transaction_id": "",
                    "error": "scripted failure"}
        new_id = f"{cmd}_{len(self.executed)}"
        self.objects.append(new_id)
        return {"ok": True, "transaction_id": f"tx{len(self.executed)}",
                "created_ids": [new_id]}

    def _execute_py(self, params):
        self.executed.append(("python", params))
        return {"ok": True, "transaction_id": "txpy", "created_ids": []}

    def phases(self):
        return [s.get("phase") for s in self.statuses]

    def messages(self, phase):
        return [s.get("message") for s in self.statuses if s.get("phase") == phase]


def _make_pair():
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    port = lst.getsockname()[1]
    holder = {}
    t = threading.Thread(target=lambda: holder.update(srv=lst.accept()[0]),
                         daemon=True)
    t.start()
    cli = socket.create_connection(("127.0.0.1", port), timeout=5)
    t.join(timeout=5)
    lst.close()
    return holder["srv"], cli


def _harness(brain, fail_cmds=None, support_groups=True):
    """Wire a real engine Session to a FakeAddon over a socket pair."""
    srv_sock, cli_sock = _make_pair()
    catalog = fake_brain.Catalog()
    eng_peer = JsonRpcPeer(FramedConnection(srv_sock), name="engine")
    session = bridge_server.Session(eng_peer, TOKEN, catalog, brain=brain)
    eng_peer.register("session.hello", session.on_hello)
    eng_peer.register("user.prompt", session.on_user_prompt)
    eng_peer.register("user.cancel", session.on_user_cancel)
    eng_peer.start()
    add_peer = JsonRpcPeer(FramedConnection(cli_sock), name="addon")
    addon = FakeAddon(add_peer, fail_cmds=fail_cmds, support_groups=support_groups)
    add_peer.start()
    hello = add_peer.call("session.hello",
                          {"token": TOKEN, "protocol_version": PROTOCOL_VERSION},
                          timeout=5)
    assert hello["ok"], "handshake failed"
    return eng_peer, add_peer, addon


def scenario_agentic_run(details):
    brain = ScriptedBrain(
        features=["create the base plate", "drill the corner hole"],
        plans=[[BOX], [HOLE]])
    eng, add, addon = _harness(brain)
    try:
        res = add.call("user.prompt", {"text": "plate with a hole"}, timeout=30)
        assert res.get("accepted") and not res.get("cancelled"), res
        assert "2 feature(s) completed" in res.get("summary", ""), res
        # Per-feature status stream ("feature i/N").
        feats = addon.messages("feature")
        assert any(m.startswith("feature 1/2") for m in feats), feats
        assert any(m.startswith("feature 2/2") for m in feats), feats
        # One undo group per feature, both COMMITTED (abort=False).
        assert addon.tx_events == [
            ("begin", "feature 1/2: create the base plate"), ("end", False),
            ("begin", "feature 2/2: drill the corner hole"), ("end", False),
        ], addon.tx_events
        # Both actions really ran, in order.
        assert [c for c, _ in addon.executed] == ["create_box", "drill_hole"], \
            addon.executed
        # The second plan() saw feature 2 AND the summary of feature 1.
        assert brain.plan_calls[0]["feature"] == "create the base plate"
        assert brain.plan_calls[1]["feature"] == "drill the corner hole"
        assert brain.plan_calls[1]["done"], "no run-state summary fed to plan 2"
        assert "create the base plate" in brain.plan_calls[1]["done"][0]
        details["agentic"] = f"{len(addon.executed)} actions, tx={addon.tx_events}"
    finally:
        add.close()
        eng.close()


def scenario_flat_fallback(details):
    brain = ScriptedBrain(features=[], plans=[[BOX]])
    eng, add, addon = _harness(brain)
    try:
        res = add.call("user.prompt", {"text": "create a box"}, timeout=30)
        assert res.get("accepted"), res
        assert "action(s) executed" in res.get("summary", ""), res
        # NO undo group in the flat flow: per-action undo exactly as in v0.12.
        assert addon.tx_events == [], addon.tx_events
        assert [c for c, _ in addon.executed] == ["create_box"], addon.executed
        # The flat plan() call carries no feature/run-state arguments.
        assert brain.plan_calls[0]["feature"] is None
        details["flat_fallback"] = "flat flow, no undo group"
    finally:
        add.close()
        eng.close()


def scenario_replan_after_rollback(details):
    # Feature 1 fails on its first plan (drill fails once, repair() gives None),
    # the group is aborted, the replan gets the feedback and then succeeds.
    brain = ScriptedBrain(
        features=["make the plate", "drill it"],
        plans=[[BOX], [HOLE], [HOLE]])  # feature1 ok; feature2 fail then ok
    eng, add, addon = _harness(brain, fail_cmds={"drill_hole": 1})
    try:
        res = add.call("user.prompt", {"text": "plate with hole"}, timeout=30)
        assert res.get("accepted"), res
        assert "2 feature(s) completed" in res.get("summary", ""), res
        # Group of feature 2 was aborted once, then committed on the replan.
        ends = [e for e in addon.tx_events if e[0] == "end"]
        assert ends == [("end", False), ("end", True), ("end", False)], \
            addon.tx_events
        # The replan received the failure as feedback.
        feedbacks = [c["feedback"] for c in brain.plan_calls]
        assert feedbacks[0] is None and feedbacks[1] is None, feedbacks
        assert feedbacks[2] and "scripted failure" in feedbacks[2], feedbacks
        assert any(p == "rollback" for p in addon.phases()), addon.phases()
        details["replan"] = f"tx events: {addon.tx_events}"
    finally:
        add.close()
        eng.close()


def scenario_stop_on_failed_feature(details):
    # Feature 1 fails on BOTH attempts -> the run stops; feature 2 never planned.
    brain = ScriptedBrain(
        features=["impossible thing", "drill it"],
        plans=[[BOX], [BOX], [HOLE]])
    eng, add, addon = _harness(brain, fail_cmds={"create_box": 99})
    try:
        res = add.call("user.prompt", {"text": "impossible then drill"}, timeout=30)
        assert res.get("accepted"), res
        assert res.get("summary", "").startswith("stopped at feature 1/2"), res
        # initial plan + 1 replan = 2 plan() calls, then STOP (bound respected).
        assert len(brain.plan_calls) == 2, brain.plan_calls
        # Both attempts aborted their group; nothing committed.
        ends = [e for e in addon.tx_events if e[0] == "end"]
        assert ends == [("end", True), ("end", True)], addon.tx_events
        # The synthetic failure result is reported.
        assert any(not r.get("ok") and "feature 1/2 failed" in r.get("error", "")
                   for r in res.get("results", [])), res.get("results")
        details["stop_on_failure"] = res.get("summary")
    finally:
        add.close()
        eng.close()


def scenario_feature_bound(details):
    # 12 proposed features -> only MAX_FEATURES (8) are run.
    n = 12
    cap = bridge_server.MAX_FEATURES
    brain = ScriptedBrain(
        features=[f"step {i}" for i in range(1, n + 1)],
        plans=[[BOX] for _ in range(n)])
    eng, add, addon = _harness(brain)
    try:
        res = add.call("user.prompt", {"text": "many steps"}, timeout=60)
        assert res.get("accepted"), res
        feats = addon.messages("feature")
        assert len(feats) == cap, f"expected {cap} features, saw {len(feats)}"
        assert any(f"first {cap}" in m for m in addon.messages("note")), \
            addon.messages("note")
        assert len(addon.executed) == cap, len(addon.executed)
        details["bound"] = f"{n} proposed -> {cap} run"
    finally:
        add.close()
        eng.close()


def scenario_degraded_old_addon(details):
    # The add-on does NOT register transaction.begin/end (a v0.12 add-on):
    # the loop still works, with per-action undo and a visible note.
    brain = ScriptedBrain(
        features=["create the base plate", "drill the corner hole"],
        plans=[[BOX], [HOLE]])
    eng, add, addon = _harness(brain, support_groups=False)
    try:
        res = add.call("user.prompt", {"text": "plate with a hole"}, timeout=30)
        assert res.get("accepted"), res
        assert "2 feature(s) completed" in res.get("summary", ""), res
        assert [c for c, _ in addon.executed] == ["create_box", "drill_hole"], \
            addon.executed
        assert any("per-feature undo not available" in m
                   for m in addon.messages("note")), addon.messages("note")
        details["degraded"] = "old add-on: loop ok, per-action undo"
    finally:
        add.close()
        eng.close()


def scenario_cancel_mid_run(details):
    # Cancel while feature 2 is being planned: nothing of feature 2 runs.
    brain = ScriptedBrain(
        features=["create the base plate", "drill the corner hole"],
        plans=[[BOX], [HOLE]],
        block_plan_at=2)  # plan() call #2 = feature 2 blocks until released
    eng, add, addon = _harness(brain)
    try:
        result = {}

        def do_prompt():
            result["res"] = add.call("user.prompt",
                                     {"text": "plate with a hole"}, timeout=30)

        t = threading.Thread(target=do_prompt, daemon=True)
        t.start()
        assert brain.plan_started.wait(timeout=10), "feature-2 plan never started"
        task_id = None
        deadline = time.time() + 3
        while time.time() < deadline and not task_id:
            for s in list(addon.statuses):
                if s.get("task_id"):
                    task_id = s["task_id"]
                    break
            time.sleep(0.02)
        assert task_id, "no task_id in statuses"
        cancel = add.call("user.cancel", {"task_id": task_id}, timeout=5)
        assert cancel.get("ok"), cancel
        brain.release.set()
        t.join(timeout=10)
        assert not t.is_alive(), "user.prompt did not return after cancel"
        res = result["res"]
        assert res.get("cancelled") is True, res
        # Feature 1 ran (and stays); feature 2 executed NOTHING.
        assert [c for c, _ in addon.executed] == ["create_box"], addon.executed
        # Feature 1's group was committed; no group was opened for feature 2.
        assert addon.tx_events == [
            ("begin", "feature 1/2: create the base plate"), ("end", False),
        ], addon.tx_events
        details["cancel"] = "cancelled during feature-2 planning, 1 action total"
    finally:
        add.close()
        eng.close()


SCENARIOS = [
    ("agentic run (2 features, undo groups, run-state)", scenario_agentic_run),
    ("flat fallback (no decomposition -> v0.12 flow)", scenario_flat_fallback),
    ("replan after rollback (feedback fed back)", scenario_replan_after_rollback),
    ("stop on failed feature (bounds respected)", scenario_stop_on_failed_feature),
    ("feature bound (MAX_FEATURES cap)", scenario_feature_bound),
    ("degraded mode (old add-on without groups)", scenario_degraded_old_addon),
    ("cancel mid-run (feature 2 never starts)", scenario_cancel_mid_run),
]


def test_feature_loop():
    details = {}
    for name, fn in SCENARIOS:
        fn(details)
    assert len(details) == len(SCENARIOS)


if __name__ == "__main__":
    print("== test_feature_loop: agentic per-feature loop (ADR 0017) ==")
    details = {}
    for name, fn in SCENARIOS:
        try:
            fn(details)
        except AssertionError as exc:
            print(f"FAIL [{name}]: {exc}")
            sys.exit(1)
        except Exception as exc:
            import traceback
            print(f"ERROR [{name}]: {exc}\n{traceback.format_exc()}")
            sys.exit(1)
        print(f"  [ok] {name}")
    print("PASS - the agentic loop plans, groups, replans, bounds and cancels "
          "as specified by ADR 0017.")
    sys.exit(0)
