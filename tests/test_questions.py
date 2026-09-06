#!/usr/bin/env python3
"""
test_questions.py - deterministic questions (ADR 0018), protocol 0.2.0.

All scenarios run WITHOUT FreeCAD and WITHOUT Ollama, over a real socket pair
(engine Session on one side, a scripted add-on on the other), with the REAL
Brain fed by a canned chat function - so the fixable-action classification in
brain._normalize is exercised end to end.

Scenarios:
  1. templates      - questions.classify / build_question unit checks (missing
                      with default = fixable; missing reference = not fixable;
                      bad enum = fixable with the allowed options).
  2. answer         - drill_hole without diameter -> user.question carries the
                      default -> the user answers 8 -> executed with 8.0.
  3. use default    - the user clicks "Use default" -> executed with 6.
  4. cancel         - the question stays unanswered, user.cancel arrives ->
                      the run is cancelled, NOTHING is executed.
  5. checkbox OFF   - ask_when_unsure=false -> NO question, the action is
                      dropped exactly like v0.12 (note in the status stream).
  6. old add-on     - user.question not registered (protocol 0.1.0 add-on) ->
                      silent default + log note, the action still runs.
  7. question cap   - 4 fixable actions -> only MAX_QUESTIONS_PER_RUN (3)
                      questions are asked, the rest use declared defaults.

Runnable:
    python tests/test_questions.py
    pytest tests/test_questions.py
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
import bridge_server   # noqa: E402
import brain as brain_mod  # noqa: E402
import fake_brain      # noqa: E402
import questions       # noqa: E402

TOKEN = "question-token"

# A plan whose only action misses 'diameter' (required, default exists -> ask).
DRILL_NO_DIAMETER = {
    "features": [],  # Brain.decompose reads this -> flat flow
    "actions": [{"type": "command", "cmd": "drill_hole",
                 "params": {"target": "Box", "depth": 10}}],
}


def canned_chat(reply):
    """A chat function that always returns the same parsed-JSON reply."""
    def chat(system, user):
        return dict(reply)
    return chat


class OfflineBrain(brain_mod.Brain):
    """Real Brain logic, but availability() does not probe a real Ollama."""

    def availability(self):
        return {"available": True, "reason": "", "models": ["fake-model"],
                "has_default_model": True, "model": "fake-model"}

    def ensure_server(self, log=None, wait_seconds=0):
        return {"status": "skipped", "launched": False, "message": "test"}


class FakeAddon:
    """Scripted add-on: records executions, statuses and questions."""

    def __init__(self, peer, question_mode=None, support_questions=True):
        """
        question_mode: None (ignore the question), "default" (click Use
        default), or any other value = answer with that value.
        """
        self.peer = peer
        self.executed = []
        self.statuses = []
        self.questions = []
        self.question_mode = question_mode
        self.question_received = threading.Event()
        self.objects = ["Box"]
        peer.register("perception.overview", lambda p: {
            "document_name": "T", "object_count": len(self.objects),
            "objects": [{"id": o, "type": "Part::Box", "label": o}
                        for o in self.objects]})
        peer.register("perception.detail", lambda p: {})
        peer.register("command.execute", self._execute)
        peer.register("agent.status", lambda p: self.statuses.append(p))
        if support_questions:
            peer.register("user.question", self._on_question)

    def _execute(self, params):
        self.executed.append((params.get("cmd"), params.get("params")))
        new_id = f"obj_{len(self.executed)}"
        self.objects.append(new_id)
        return {"ok": True, "transaction_id": f"tx{len(self.executed)}",
                "created_ids": [new_id]}

    def _on_question(self, params):
        self.questions.append(params)
        self.question_received.set()
        if self.question_mode is not None:
            qid = params.get("question_id")
            mode = self.question_mode

            def reply():
                time.sleep(0.05)
                if mode == "default":
                    self.peer.call("user.answer",
                                   {"question_id": qid, "use_default": True},
                                   timeout=5)
                else:
                    self.peer.call("user.answer",
                                   {"question_id": qid, "value": mode},
                                   timeout=5)
            threading.Thread(target=reply, daemon=True).start()
        return {"ok": True}

    def messages(self, phase):
        return [s.get("message") for s in self.statuses if s.get("phase") == phase]

    def wait_message(self, phase, needle, timeout=3.0):
        """Poll for a status message (notifications are asynchronous: they can
        arrive a moment AFTER the user.prompt response - same socket, but the
        peer dispatches them on a pool thread)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = self.messages(phase)
            if any(needle in m for m in msgs):
                return msgs
            time.sleep(0.02)
        return self.messages(phase)


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


def _harness(reply, question_mode=None, support_questions=True):
    """Real engine Session + real Brain (canned chat) vs a scripted add-on."""
    srv_sock, cli_sock = _make_pair()
    catalog = fake_brain.Catalog()
    the_brain = OfflineBrain(catalog=catalog, chat=canned_chat(reply))
    eng_peer = JsonRpcPeer(FramedConnection(srv_sock), name="engine")
    session = bridge_server.Session(eng_peer, TOKEN, catalog, brain=the_brain)
    eng_peer.register("session.hello", session.on_hello)
    eng_peer.register("user.prompt", session.on_user_prompt)
    eng_peer.register("user.cancel", session.on_user_cancel)
    eng_peer.register("user.answer", session.on_user_answer)
    eng_peer.start()
    add_peer = JsonRpcPeer(FramedConnection(cli_sock), name="addon")
    addon = FakeAddon(add_peer, question_mode=question_mode,
                      support_questions=support_questions)
    add_peer.start()
    hello = add_peer.call("session.hello",
                          {"token": TOKEN, "protocol_version": PROTOCOL_VERSION},
                          timeout=5)
    assert hello["ok"], "handshake failed"
    return eng_peer, add_peer, addon


def scenario_templates(details):
    cat = fake_brain.Catalog()
    # Missing required param WITH a default -> fixable.
    info = questions.classify(
        {"cmd": "drill_hole", "params": {"target": "Box", "depth": 10}}, cat)
    assert info["fixable"] and info["missing"] == ["diameter"], info
    # Missing OBJECT REFERENCE (no default possible) -> NOT fixable.
    info2 = questions.classify(
        {"cmd": "drill_hole", "params": {"diameter": 6, "depth": 10}}, cat)
    assert not info2["fixable"], info2
    # Bad enum value -> fixable, with the allowed options in the question.
    info3 = questions.classify(
        {"cmd": "fillet", "params": {"target": "Box", "radius": 2,
                                     "where": "sides"}}, cat)
    assert info3["fixable"] and "where" in info3["bad_enum"], info3
    q = questions.build_question("fillet", "where", cat, bad_value="sides")
    assert q and "sides" in q["question"] and "all" in q["options"], q
    # A violated minimum is NOT a question (classic drop/repair path).
    info4 = questions.classify(
        {"cmd": "create_box", "params": {"length": -1}}, cat)
    assert not info4["fixable"], info4
    # Numeric question carries the default and a unit hint.
    q2 = questions.build_question("drill_hole", "diameter", cat)
    assert q2["default"] == 6 and "mm" in q2["question"], q2
    # Answers are coerced to the parameter type.
    assert questions.coerce_answer("8", "drill_hole", "diameter", cat) == 8.0
    assert questions.coerce_answer("5", "array", "count", cat) == 5
    details["templates"] = "classify/build/coerce ok"


def scenario_answer(details):
    eng, add, addon = _harness(DRILL_NO_DIAMETER, question_mode="8")
    try:
        res = add.call("user.prompt", {"text": "drill a hole in the box"},
                       timeout=30)
        assert res.get("accepted") and not res.get("cancelled"), res
        assert len(addon.questions) == 1, addon.questions
        q = addon.questions[0]
        assert q.get("cmd") == "drill_hole" and q.get("param") == "diameter", q
        assert q.get("default") == 6 and q.get("question_id"), q
        assert q.get("options"), q
        assert len(addon.executed) == 1, addon.executed
        cmd, params = addon.executed[0]
        assert cmd == "drill_hole" and params.get("diameter") == 8.0, \
            addon.executed
        details["answer"] = f"asked, answered 8 -> {params}"
    finally:
        add.close()
        eng.close()


def scenario_use_default(details):
    eng, add, addon = _harness(DRILL_NO_DIAMETER, question_mode="default")
    try:
        res = add.call("user.prompt", {"text": "drill a hole in the box"},
                       timeout=30)
        assert res.get("accepted"), res
        assert len(addon.questions) == 1, addon.questions
        cmd, params = addon.executed[0]
        assert cmd == "drill_hole" and params.get("diameter") == 6, \
            addon.executed
        details["use_default"] = f"default applied -> {params}"
    finally:
        add.close()
        eng.close()


def scenario_cancel_during_question(details):
    # The add-on shows the question but the user cancels instead of answering.
    eng, add, addon = _harness(DRILL_NO_DIAMETER, question_mode=None)
    try:
        result = {}

        def do_prompt():
            result["res"] = add.call("user.prompt",
                                     {"text": "drill a hole"}, timeout=30)

        t = threading.Thread(target=do_prompt, daemon=True)
        t.start()
        assert addon.question_received.wait(timeout=10), "no question arrived"
        task_id = addon.questions[0].get("task_id")
        assert task_id, addon.questions[0]
        cancel = add.call("user.cancel", {"task_id": task_id}, timeout=5)
        assert cancel.get("ok"), cancel
        t.join(timeout=10)
        assert not t.is_alive(), "user.prompt did not return after cancel"
        res = result["res"]
        assert res.get("cancelled") is True, res
        assert addon.executed == [], addon.executed
        details["cancel"] = "question pending, cancel -> nothing executed"
    finally:
        add.close()
        eng.close()


def scenario_checkbox_off(details):
    # ask_when_unsure=false -> v0.12 identical: no question, action dropped.
    eng, add, addon = _harness(DRILL_NO_DIAMETER, question_mode="8")
    try:
        res = add.call("user.prompt",
                       {"text": "drill a hole", "ask_when_unsure": False},
                       timeout=30)
        assert res.get("accepted"), res
        assert addon.questions == [], addon.questions
        assert addon.executed == [], addon.executed
        assert res.get("clarification"), res  # no runnable action left
        notes = addon.wait_message("note", "dropped")
        assert any("dropped" in n and "diameter" in n for n in notes), notes
        details["checkbox_off"] = "no question, dropped like v0.12"
    finally:
        add.close()
        eng.close()


def scenario_old_addon(details):
    # The add-on does NOT know user.question (protocol 0.1.0): the engine
    # degrades to a silent default + log, and the action still runs.
    eng, add, addon = _harness(DRILL_NO_DIAMETER, support_questions=False)
    try:
        res = add.call("user.prompt", {"text": "drill a hole"}, timeout=30)
        assert res.get("accepted"), res
        assert len(addon.executed) == 1, addon.executed
        cmd, params = addon.executed[0]
        assert params.get("diameter") == 6, addon.executed
        notes = addon.wait_message("note", "assuming drill_hole.diameter")
        assert any("assuming drill_hole.diameter = 6" in n for n in notes), notes
        details["old_addon"] = "silent default + log, action executed"
    finally:
        add.close()
        eng.close()


def scenario_question_cap(details):
    # 4 fixable actions but MAX_QUESTIONS_PER_RUN=3: the 4th uses the default.
    plan = {
        "features": [],
        "actions": [
            {"type": "command", "cmd": "create_box",
             "params": {"length": 30, "width": 20}},              # height?
            {"type": "command", "cmd": "create_cylinder",
             "params": {"radius": 5}},                            # height?
            {"type": "command", "cmd": "drill_hole",
             "params": {"target": "Box", "depth": 5}},            # diameter?
            {"type": "command", "cmd": "fillet",
             "params": {"target": "Box"}},                        # radius?
        ],
    }
    eng, add, addon = _harness(plan, question_mode="7")
    try:
        res = add.call("user.prompt", {"text": "several things"}, timeout=60)
        assert res.get("accepted"), res
        cap = bridge_server.MAX_QUESTIONS_PER_RUN
        assert len(addon.questions) == cap, \
            f"expected {cap} questions, saw {len(addon.questions)}"
        assert len(addon.executed) == 4, addon.executed
        notes = addon.wait_message("note", "question cap reached")
        assert any("question cap reached" in n for n in notes), notes
        # The capped parameter got its declared default (fillet.radius = 2).
        fillet_params = [p for c, p in addon.executed if c == "fillet"][0]
        assert fillet_params.get("radius") == 2, fillet_params
        details["cap"] = f"{cap} asked, 4th defaulted"
    finally:
        add.close()
        eng.close()


SCENARIOS = [
    ("templates (classify/build/coerce)", scenario_templates),
    ("question -> answer", scenario_answer),
    ("question -> use default", scenario_use_default),
    ("question -> cancel", scenario_cancel_during_question),
    ("checkbox OFF = v0.12", scenario_checkbox_off),
    ("old add-on -> silent default", scenario_old_addon),
    ("question cap per run", scenario_question_cap),
]


def test_questions():
    details = {}
    for name, fn in SCENARIOS:
        fn(details)
    assert len(details) == len(SCENARIOS)


if __name__ == "__main__":
    print("== test_questions: deterministic questions (ADR 0018) ==")
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
    print("PASS - the engine asks, waits, defaults, respects the cap and the "
          "checkbox, and degrades with an old add-on (ADR 0018).")
    sys.exit(0)
