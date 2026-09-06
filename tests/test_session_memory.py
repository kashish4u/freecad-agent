#!/usr/bin/env python3
"""
test_session_memory.py - model-side questions + session memory (ADR 0019).

All scenarios run WITHOUT FreeCAD and WITHOUT Ollama, with the REAL Brain fed
by a scripted chat function (so prompt construction and normalization are
exercised end to end) wired to a real engine Session over a socket pair.

Scenarios:
  1. model ask      - the model returns {"ask": ...}; the engine forwards it
                      via user.question, the user answers, the engine REPLANS
                      with "(Clarified: Q -> A)" folded into the text, and the
                      corrected plan runs.
  2. ask no default - an ask without a default NEVER blocks: it degrades to a
                      plain clarification (golden rule).
  3. ask loop bound - a model that asks forever is stopped by the per-run cap
                      (no infinite ask/replan loop).
  4. memory in the prompt - after "create a cylinder", a second request sees
                      an EARLIER IN THIS SESSION block with the first request
                      and its created ids (simulated anaphora: the model can
                      resolve "drill it" from it).
  5. sliding window - only the last MEMORY_MAX_LINES runs are remembered.
  6. ask OFF        - with ask_when_unsure=false the model's ask is ignored
                      and surfaced as a clarification (v0.12-style outcome).

Runnable:
    python tests/test_session_memory.py
    pytest tests/test_session_memory.py
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

TOKEN = "memory-token"

CYL = {"type": "command", "cmd": "create_cylinder",
       "params": {"radius": 20, "height": 50}}
HOLE_IT = {"type": "command", "cmd": "drill_hole",
           "params": {"target": "Cylinder", "diameter": 6, "depth": 50}}


class ScriptedChat:
    """
    Dispatching chat function for the REAL Brain: decompose calls (recognised
    by their tiny system prompt) always return no features; plan calls pop the
    next scripted reply (the last one repeats). Records every plan prompt.
    """

    def __init__(self, plan_replies):
        self.plan_replies = list(plan_replies)
        self.plan_prompts = []      # (system, user) of every plan call

    def __call__(self, system, user):
        if "break a CAD modelling request" in system:
            return {"features": []}
        self.plan_prompts.append((system, user))
        if len(self.plan_replies) > 1:
            return self.plan_replies.pop(0)
        return dict(self.plan_replies[0])


class OfflineBrain(brain_mod.Brain):
    """Real Brain logic; availability() does not probe a real Ollama."""

    def availability(self):
        return {"available": True, "reason": "", "models": ["fake-model"],
                "has_default_model": True, "model": "fake-model"}

    def ensure_server(self, log=None, wait_seconds=0):
        return {"status": "skipped", "launched": False, "message": "test"}


class FakeAddon:
    """Scripted add-on: executes everything, can auto-answer questions."""

    def __init__(self, peer, question_mode=None):
        self.peer = peer
        self.executed = []
        self.statuses = []
        self.questions = []
        self.question_mode = question_mode   # None=ignore, "default", value
        self.question_received = threading.Event()
        self.objects = []
        peer.register("perception.overview", lambda p: {
            "document_name": "T", "object_count": len(self.objects),
            "objects": [{"id": o, "type": "Part::Feature", "label": o}
                        for o in self.objects]})
        peer.register("perception.detail", lambda p: {})
        peer.register("command.execute", self._execute)
        peer.register("agent.status", lambda p: self.statuses.append(p))
        peer.register("user.question", self._on_question)

    def _execute(self, params):
        self.executed.append((params.get("cmd"), params.get("params")))
        # Name the object after the command's usual FreeCAD id.
        base = {"create_cylinder": "Cylinder", "create_box": "Box",
                "drill_hole": "Drilled"}.get(params.get("cmd"), "Obj")
        new_id = base if base not in self.objects else f"{base}001"
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


def _harness(chat, question_mode=None):
    srv_sock, cli_sock = _make_pair()
    catalog = fake_brain.Catalog()
    the_brain = OfflineBrain(catalog=catalog, chat=chat)
    eng_peer = JsonRpcPeer(FramedConnection(srv_sock), name="engine")
    session = bridge_server.Session(eng_peer, TOKEN, catalog, brain=the_brain)
    eng_peer.register("session.hello", session.on_hello)
    eng_peer.register("user.prompt", session.on_user_prompt)
    eng_peer.register("user.cancel", session.on_user_cancel)
    eng_peer.register("user.answer", session.on_user_answer)
    eng_peer.start()
    add_peer = JsonRpcPeer(FramedConnection(cli_sock), name="addon")
    addon = FakeAddon(add_peer, question_mode=question_mode)
    add_peer.start()
    hello = add_peer.call("session.hello",
                          {"token": TOKEN, "protocol_version": PROTOCOL_VERSION},
                          timeout=5)
    assert hello["ok"], "handshake failed"
    return eng_peer, add_peer, addon, session


def scenario_model_ask(details):
    # Plan 1: the model asks; plan 2 (after the answer): it acts.
    chat = ScriptedChat([
        {"actions": [], "ask": {"question": "What diameter for the hole?",
                                "options": ["5", "6", "8"], "default": 6}},
        {"actions": [{"type": "command", "cmd": "create_box",
                      "params": {"length": 10, "width": 10, "height": 10}}]},
    ])
    eng, add, addon, _ = _harness(chat, question_mode="8")
    try:
        res = add.call("user.prompt", {"text": "drill a mounting hole"},
                       timeout=30)
        assert res.get("accepted") and not res.get("cancelled"), res
        # The question travelled through user.question with the model's text.
        assert len(addon.questions) == 1, addon.questions
        assert "diameter" in addon.questions[0]["question"], addon.questions
        assert addon.questions[0]["default"] == 6, addon.questions
        # The REPLAN saw the clarification folded into the request text.
        assert len(chat.plan_prompts) == 2, len(chat.plan_prompts)
        replan_user = chat.plan_prompts[1][1]
        assert "(Clarified: What diameter for the hole? -> 8)" in replan_user, \
            replan_user
        # And the second plan's action ran.
        assert [c for c, _ in addon.executed] == ["create_box"], addon.executed
        details["model_ask"] = "ask -> answer -> replan -> executed"
    finally:
        add.close()
        eng.close()


def scenario_ask_without_default(details):
    # Golden rule: no default => never a blocking wait, just a clarification.
    chat = ScriptedChat([
        {"actions": [], "ask": {"question": "Which standard applies?"}},
    ])
    eng, add, addon, _ = _harness(chat, question_mode="8")
    try:
        res = add.call("user.prompt", {"text": "make the flange to standard"},
                       timeout=30)
        assert res.get("accepted"), res
        assert addon.questions == [], addon.questions   # never asked
        assert res.get("clarification") == "Which standard applies?", res
        assert addon.executed == [], addon.executed
        details["no_default"] = "surfaced as clarification, no wait"
    finally:
        add.close()
        eng.close()


def scenario_ask_loop_bound(details):
    # A model that ALWAYS asks must not loop forever: per-run cap applies.
    chat = ScriptedChat([
        {"actions": [], "ask": {"question": "Are you sure?", "default": "yes"}},
    ])
    eng, add, addon, _ = _harness(chat, question_mode="default")
    try:
        res = add.call("user.prompt", {"text": "do something"}, timeout=60)
        assert res.get("accepted"), res
        cap = bridge_server.MAX_QUESTIONS_PER_RUN
        assert len(addon.questions) == cap, \
            f"expected {cap} asks, saw {len(addon.questions)}"
        # 1 initial plan + one replan per settled ask.
        assert len(chat.plan_prompts) == 1 + cap, len(chat.plan_prompts)
        assert res.get("clarification"), res   # still nothing runnable
        details["loop_bound"] = f"stopped after {cap} asks"
    finally:
        add.close()
        eng.close()


def scenario_memory_in_prompt(details):
    # Run 1 creates a cylinder; run 2's prompt must carry the session memory
    # (the simulated anaphora: the model of run 2 targets that cylinder).
    chat = ScriptedChat([
        {"actions": [CYL]},
        {"actions": [HOLE_IT]},
    ])
    eng, add, addon, session = _harness(chat)
    try:
        res1 = add.call("user.prompt",
                        {"text": "create a cylinder r20 h50"}, timeout=30)
        assert res1.get("accepted"), res1
        assert session._history and "create a cylinder r20 h50" in \
            session._history[0], session._history
        assert "Cylinder" in session._history[0], session._history
        res2 = add.call("user.prompt",
                        {"text": "drill a 6 mm hole in its centre"}, timeout=30)
        assert res2.get("accepted"), res2
        user2 = chat.plan_prompts[1][1]
        assert "EARLIER IN THIS SESSION" in user2, user2
        assert "create a cylinder r20 h50" in user2, user2
        assert "Pronouns" in user2, user2
        # The anaphoric action ran against the cylinder.
        assert addon.executed[1][0] == "drill_hole", addon.executed
        assert addon.executed[1][1].get("target") == "Cylinder", addon.executed
        # First run had NO memory block (empty history).
        assert "EARLIER IN THIS SESSION" not in chat.plan_prompts[0][1]
        details["memory"] = session._history[-1]
    finally:
        add.close()
        eng.close()


def scenario_sliding_window(details):
    chat = ScriptedChat([{"actions": [CYL]}])
    eng, add, addon, session = _harness(chat)
    try:
        n = bridge_server.MEMORY_MAX_LINES + 3
        for i in range(n):
            add.call("user.prompt", {"text": f"request number {i}"}, timeout=30)
        assert len(session._history) == bridge_server.MEMORY_MAX_LINES, \
            len(session._history)
        assert f"request number {n - 1}" in session._history[-1], \
            session._history
        assert "request number 0" not in "".join(session._history)
        details["window"] = f"{n} runs -> {len(session._history)} lines kept"
    finally:
        add.close()
        eng.close()


def scenario_ask_off(details):
    # ask_when_unsure=false: the model's ask is ignored, no question shown.
    chat = ScriptedChat([
        {"actions": [], "ask": {"question": "What diameter?", "default": 6}},
    ])
    eng, add, addon, _ = _harness(chat, question_mode="8")
    try:
        res = add.call("user.prompt",
                       {"text": "drill a hole", "ask_when_unsure": False},
                       timeout=30)
        assert res.get("accepted"), res
        assert addon.questions == [], addon.questions
        assert addon.executed == [], addon.executed
        assert res.get("clarification") == "What diameter?", res
        details["ask_off"] = "ask ignored, surfaced as clarification"
    finally:
        add.close()
        eng.close()


def scenario_ask_yields_to_templated(details):
    # Sess.19 collaudo fix: the model raises a VAGUE ask, but the same plan
    # already carries a FIXABLE action (drill_hole missing 'diameter', which
    # the deterministic layer can template). The templated question must WIN:
    # the model's ask is dropped (no fragile replan), and the user's numeric
    # answer is applied directly as diameter=8.0. This is the exact failure
    # seen in real testing ("asked 8, drilled 6").
    chat = ScriptedChat([
        {"actions": [{"type": "command", "cmd": "drill_hole",
                      "params": {"target": "Box", "depth": 15}}],
         "ask": {"question": "What size and position of hole?",
                 "options": ["6"], "default": 6}},
    ])
    eng, add, addon, _ = _harness(chat, question_mode="8")
    try:
        res = add.call("user.prompt", {"text": "drill a hole in the box"},
                       timeout=30)
        assert res.get("accepted") and not res.get("cancelled"), res
        # Exactly ONE question, and it is the TEMPLATED diameter one - NOT the
        # model's vague ask.
        assert len(addon.questions) == 1, addon.questions
        q = addon.questions[0]
        assert q.get("cmd") == "drill_hole" and q.get("param") == "diameter", q
        # No replan: the model ask was dropped, so a single plan call happened.
        assert len(chat.plan_prompts) == 1, len(chat.plan_prompts)
        # The answer 8 was applied deterministically (not lost in a replan).
        assert len(addon.executed) == 1, addon.executed
        cmd, params = addon.executed[0]
        assert cmd == "drill_hole", addon.executed
        assert float(params.get("diameter")) == 8.0, params
        details["ask_yields"] = "model ask dropped; templated q applied 8.0"
    finally:
        add.close()
        eng.close()


SCENARIOS = [
    ("model ask -> answer -> replan", scenario_model_ask),
    ("templated question beats a vague model ask (Sess.19 fix)",
     scenario_ask_yields_to_templated),
    ("ask without default = clarification", scenario_ask_without_default),
    ("ask loop bound (per-run cap)", scenario_ask_loop_bound),
    ("session memory in the prompt (anaphora)", scenario_memory_in_prompt),
    ("sliding window", scenario_sliding_window),
    ("checkbox OFF ignores the ask", scenario_ask_off),
]


def test_session_memory():
    details = {}
    for name, fn in SCENARIOS:
        fn(details)
    assert len(details) == len(SCENARIOS)


if __name__ == "__main__":
    print("== test_session_memory: model asks + session memory (ADR 0019) ==")
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
    print("PASS - the model can ask (bounded, defaulted), and the session "
          "memory feeds the prompt (ADR 0019).")
    sys.exit(0)
