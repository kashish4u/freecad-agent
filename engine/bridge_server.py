#!/usr/bin/env python3
"""
engine/bridge_server.py - PERSISTENT ENGINE of Phase 1 (still WITHOUT AI).

Difference from Phase 1a: it no longer runs a one-shot demo and no longer exits.
It keeps listening (accept loop), handles multiple commands per session, survives
add-on disconnections (it goes back to waiting), and shuts down cleanly (Ctrl+C)
removing the discovery file.

TWO RUN MODES (ADR 0015 "engine lifecycle" + topology flip):
  - CLIENT mode (PRODUCTION, default when the add-on launches us): if we receive
    --host/--port/--token (or the FREECAD_AGENT_HOST/PORT/TOKEN env vars), the
    ADD-ON is the TCP server; we connect to it and present the token via
    session.hello (the add-on validates it). No discovery file. This is the
    topology ADR 0002 foresaw for production; the bridge core is symmetric
    (ADR 0001), so only the handshake roles swap - all the operational handlers
    keep their direction.
  - SERVER standalone mode (DEBUG, when launched with no connection args, e.g.
    from START_ENGINE.bat): we are the TCP server on 127.0.0.1 with an ephemeral
    port + token and we write the discovery file so an add-on in "attach (debug)"
    mode can find us. This is the historical prototype behaviour, kept for our
    own debugging.

Role (see ADR 0002 + ADR 0003):
  - As the "fake brain" it receives an ALREADY-structured command from
    the add-on panel via `command.request`, VALIDATES it against
    commands.schema.json (fake_brain) and, if valid, forwards it to the add-on as
    `command.execute` (exercising both directions of the bridge). It emits
    `agent.status`.
  - `user.prompt` (natural language) is NOT implemented in Phase 1: a polite
    refusal (the AI arrives in Phase 2).

Threading (RISK #2): the peer runs incoming handlers on an internal pool (see
shared/bridge/jsonrpc.py), so the `command.request` handler can call
`command.execute` back on the add-on without blocking the read loop (ADR 0003).

Environment variables (for automated tests):
  FREECAD_AGENT_ACCEPT_TIMEOUT  seconds to wait for EACH connection (default: none = infinite)
  FREECAD_AGENT_ONESHOT         if "1", serve a SINGLE connection then exit (for tests)

Start (Windows): double-click START_ENGINE.bat, or:
    cd freecad-agent\\engine
    python bridge_server.py
"""

from __future__ import annotations

import inspect
import os
import socket
import sys
import threading
import time
from pathlib import Path

# --- Import the neutral bridge library (shared/bridge) ------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "shared"))

from bridge import (  # noqa: E402
    FramedConnection,
    JsonRpcPeer,
    JsonRpcError,
    ConnectionClosed,
    ErrorCode,
    discovery,
    PROTOCOL_VERSION,
)
import fake_brain  # noqa: E402  (engine/fake_brain.py, same folder)
import questions as questions_mod  # noqa: E402  (templated questions, ADR 0018)
from brain import Brain, PlanError  # noqa: E402  (the real Phase 2 planning brain)
from ollama_client import OllamaUnavailable  # noqa: E402

ENGINE_VERSION = "0.13.0"

# Phase 3 "geometric RAG": before planning we fetch perception.detail for the
# existing objects so the model sees real Edge*/Face* references. We only do this
# for SMALL documents, to stay concise for small local models (principle 9). For
# bigger documents we fall back to the cheap overview only.
MAX_DETAIL_OBJECTS = 8
# ADR 0016: commands whose new feature CONSUMES the object(s) these params
# reference (Part::Cut / boolean / dress-up features swallow their base, which
# survives only as a hidden child). Within one plan, a later step that still
# names the OLD id really means the NEW result: we redirect it. mirror/array
# keep their source visible and move/rotate keep the same id, so they are not
# listed. (extrude consumes its sketch profile, ADR 0009/0010.)
CONSUMING_PARAMS = {
    "drill_hole": ("target",),
    "boolean": ("a", "b"),
    "fillet": ("target",),
    "chamfer": ("target",),
    "extrude": ("target",),
    "revolve": ("target",),   # consumes its sketch profile, like extrude
    "loft": ("profiles",),    # consumes a LIST of profiles (ADR 0021)
    "sweep": ("profile", "path"),
    "shell": ("target",),     # Part::Thickness swallows its base solid
}
# Commands that consume a freshly created sketch profile as their `target`
# (ADR 0010 chaining: the model cannot predict 'Sketch001'). extrude since
# Phase 5; revolve added in Phase 7 (same semantics).
PROFILE_CONSUMERS = ("extrude", "revolve")
# Commands that consume MULTIPLE profiles (ADR 0021): their unknown profile
# references are rewritten, in order, to the sketches created in this run and
# not yet consumed (oldest first).
LIST_PROFILE_CONSUMERS = ("loft", "sweep")
# Bounded self-correction (principle 8: no infinite loops).
MAX_REPAIR_ATTEMPTS = 2
# ADR 0017 (agentic per-feature loop) bounds, same philosophy: hard caps so a
# small model can never trap the engine in an endless run.
MAX_FEATURES = 8              # features per run (beyond ~6-8 a 4B drifts anyway)
MAX_REPLANS_PER_FEATURE = 1   # replans after a feature failed (on top of the
                              # per-action MAX_REPAIR_ATTEMPTS)
# ADR 0018 (deterministic questions). Questions are CLARIFICATIONS, never
# permission requests (principle 4 intact): each carries a default the user can
# accept with one click. Hard cap per RUN (not per feature plan: an 8-feature
# agentic run must not turn into an interrogation); past the cap the engine
# proceeds with the declared defaults.
MAX_QUESTIONS_PER_RUN = 3
# Safety net for an unanswered question: after this many seconds the engine
# logs it and proceeds with the default (the wait itself is a cancellable
# checkpoint in the ADR 0008 style, polled in small slices).
QUESTION_WAIT_TIMEOUT = 300.0
QUESTION_POLL_SLICE = 0.25
# ADR 0019 (session memory). The engine keeps a COMPRESSED sliding window of
# this session's earlier requests (one line each: request -> outcome + created
# ids) and feeds it to the model, so "now drill it in the centre" resolves
# against the cylinder made two requests ago. Summaries, never transcripts
# (ctx-8192 realism); nothing is persisted to disk (privacy: the memory dies
# with the engine process).
MEMORY_MAX_LINES = 8
MEMORY_LINE_MAX = 160          # chars per line (the brain re-caps at render)


def log(msg: str) -> None:
    print(f"[engine] {msg}", flush=True)


class _RunState:
    """
    State of ONE natural-language run (ADR 0017). It bundles the id-chaining
    state that used to live as locals of a single flat plan (ADR 0010/0013/0016)
    so it can persist ACROSS features in the agentic loop, plus the accumulated
    results and the per-feature summary lines fed back to the model.

    snapshot()/restore() bracket a feature execution: when the feature's undo
    group is aborted (rollback), the chaining state must forget the ids the
    rolled-back actions created, or later steps would chain onto ghosts.
    """

    def __init__(self, known_ids: "set[str]") -> None:
        self.last_profile_id = None    # sketch awaiting its extrude (ADR 0010)
        self.last_created_id = None    # last object created (ADR 0013)
        self.created_count = 0         # creations so far (ADR 0013 guard)
        self.known_ids: "set[str]" = set(known_ids)   # ids the model may reference
        self.replaced: dict = {}       # consumed id -> live result (ADR 0016)
        self.results: list = []        # commandResults of every action run
        self.done_features: "list[str]" = []  # one line per finished feature
        # ADR 0021: sketches created in this run and NOT yet consumed, in
        # creation order - the pool multi-profile commands (loft/sweep) draw
        # from when the model's profile names do not resolve.
        self.pending_profiles: "list[str]" = []

    def snapshot(self) -> tuple:
        """Cheap copy of the chaining state (results by LENGTH, they only grow).
        NOTE: the results-length stays the LAST element (snap[-1]) - the agentic
        loop slices state.results with it."""
        return (self.last_profile_id, self.last_created_id, self.created_count,
                set(self.known_ids), dict(self.replaced),
                list(self.pending_profiles), len(self.results))

    def restore(self, snap: tuple) -> None:
        """Roll the state back to a snapshot (after an aborted undo group)."""
        (self.last_profile_id, self.last_created_id, self.created_count,
         self.known_ids, self.replaced, self.pending_profiles,
         n_results) = snap
        del self.results[n_results:]


class Session:
    """
    A session = one add-on connection. It owns the peer and the handlers.
    The "fake brain" lives here: it validates commands and forwards them to the
    add-on.
    """

    def __init__(self, peer: JsonRpcPeer, token: str, catalog: fake_brain.Catalog,
                 brain: "Brain | None" = None) -> None:
        self.peer = peer
        self.token = token
        self.catalog = catalog
        # The real planning brain (Phase 2). Injectable so headless tests can pass
        # a fake one; by default it talks to a local Ollama (ADR 0004).
        self.brain = brain or Brain(catalog)
        self.authenticated = threading.Event()
        self._task_counter = 0
        # Phase 4 (ADR 0007): try a "lazy" Ollama auto-start at most once per
        # session, the first time natural language is used while it is down.
        self._ollama_autostart_tried = False
        # Phase 4 (ADR 0008): cooperative cancellation. user.cancel(task_id) just
        # records the id here; the running on_user_prompt loop checks it at safe
        # checkpoints and stops without executing any further FreeCAD action. We
        # cannot abort an in-flight model inference, so cancel takes effect at the
        # NEXT checkpoint (correctness over speed - principle 8).
        self._cancel_lock = threading.Lock()
        self._cancelled_tasks: "set[str]" = set()
        # ADR 0017: does the add-on support per-feature undo groups
        # (transaction.begin/end)? Assumed yes until the first call fails, then
        # we degrade to per-action transactions for the rest of the session.
        self._tx_supported = True
        # ADR 0018: deterministic questions. The add-on is assumed to support
        # user.question until the first call fails (older add-on), then we
        # degrade to silent defaults + log for the rest of the session.
        self._questions_supported = True
        self._question_lock = threading.Lock()
        self._question_counter = 0
        self._answers: "dict[str, dict]" = {}       # question_id -> answer
        self._answer_events: "dict[str, threading.Event]" = {}
        # Per-run state, reset at each user.prompt: is asking enabled (panel
        # checkbox "Ask me when unsure"), and how many questions we asked.
        self._ask_enabled = True
        self._questions_asked = 0
        # ADR 0019: compressed session memory (sliding window, in RAM only).
        self._history: "list[str]" = []

    # -- handshake -------------------------------------------------------------

    def on_hello(self, params: dict) -> dict:
        """The add-on (client) presents the token; we validate it (see ADR 0002)."""
        client_token = params.get("token", "")
        client_proto = params.get("protocol_version", "?")
        if client_token != self.token:
            log("handshake REJECTED: wrong token")
            raise JsonRpcError(ErrorCode.AUTH_FAILED, "invalid token")
        if client_proto != PROTOCOL_VERSION:
            log(f"WARNING: protocol_version addon={client_proto} engine={PROTOCOL_VERSION}")
        self.authenticated.set()
        version, names = fake_brain.summarize(self.catalog)
        log(f"handshake OK (addon proto={client_proto}). Vocabulary v{version}: {', '.join(names)}")
        return {
            "ok": True,
            "engine_version": ENGINE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "vocabulary_version": version,
            "commands": names,
        }

    # -- main Phase 1 channel: structured command from the panel ---------------

    def on_command_request(self, params: dict) -> dict:
        """
        Receives a commandInvocation {cmd, params}. Validates it and, if valid,
        forwards it to the add-on as `command.execute`. Returns a commandResult.

        Runs on a pool thread (the peer's internal pool), so it can make the
        nested `command.execute` call without deadlocking.
        """
        self._task_counter += 1
        task_id = f"task-{self._task_counter:04d}"
        cmd = params.get("cmd") if isinstance(params, dict) else None
        log(f"[{task_id}] command.request: {cmd} {params.get('params') if isinstance(params, dict) else ''}")

        # 1) VALIDATION (the fake brain's job)
        self._notify(task_id, "validation", f"checking '{cmd}' against the vocabulary")
        errors = fake_brain.validate_invocation(params, self.catalog)
        if fake_brain.is_blocking(errors):
            msg = "; ".join(errors)
            log(f"[{task_id}] REJECTED (validation): {msg}")
            self._notify(task_id, "rejected", msg)
            # Graceful refusal (principle 7): commandResult ok=false, no exception.
            return {"ok": False, "transaction_id": "", "error": f"validation failed: {msg}"}
        if errors:  # non-blocking warnings only
            log(f"[{task_id}] warnings: {'; '.join(errors)}")

        # 2) EXECUTION: forward to the add-on (engine -> addon)
        self._notify(task_id, "execution", f"running '{cmd}' on FreeCAD", privacy="local")
        try:
            result = self.peer.call("command.execute", params, timeout=60)
        except (JsonRpcError, TimeoutError) as exc:
            log(f"[{task_id}] error during command.execute: {exc}")
            self._notify(task_id, "error", str(exc))
            return {"ok": False, "transaction_id": "", "error": f"execution failed: {exc}"}

        ok = bool(result.get("ok"))
        if ok:
            log(f"[{task_id}] OK: tx={result.get('transaction_id')} objects={result.get('created_ids')}")
            self._notify(task_id, "completed",
                         f"created: {result.get('created_ids')} (Ctrl+Z to undo)")
        else:
            log(f"[{task_id}] execution failed on the add-on side: {result.get('error')}")
            self._notify(task_id, "error", str(result.get("error")))
        return result

    # -- natural-language channel: the real AI agent (Phase 2) -----------------

    def on_user_prompt(self, params: dict) -> dict:
        """
        Entry point for user.prompt. Delegates to _handle_user_prompt and then
        records a one-line summary of the run in the session memory (ADR 0019)
        so the NEXT request can refer back to it ("now drill it").
        """
        result = self._handle_user_prompt(params)
        try:
            self._remember_run(params, result)
        except Exception:  # pragma: no cover - memory must never break a run
            pass
        return result

    # -- session memory (ADR 0019) ----------------------------------------------

    def _remember_run(self, params, result) -> None:
        """Append 'request -> outcome [created: ids]' to the sliding window."""
        if not isinstance(params, dict) or not isinstance(result, dict):
            return
        text = (params.get("text") or "").strip()
        if not text:
            return
        if result.get("cancelled"):
            outcome = "cancelled by the user"
        elif not result.get("accepted"):
            outcome = f"failed ({str(result.get('error', ''))[:60]})"
        elif result.get("clarification"):
            outcome = f"needed more info ({str(result.get('clarification'))[:60]})"
        else:
            outcome = result.get("summary") or "done"
        created = [cid
                   for r in (result.get("results") or [])
                   if isinstance(r, dict) and r.get("ok")
                   for cid in (r.get("created_ids") or [])]
        line = f'"{text[:80]}" -> {outcome}'
        if created:
            line += f" [created: {', '.join(created[:6])}]"
        self._history.append(line[:MEMORY_LINE_MAX])
        del self._history[:-MEMORY_MAX_LINES]

    # -- brain call helpers (ADR 0019) -------------------------------------------
    # The history kwarg is new: stub brains in old tests (and any third-party
    # brain) may not accept it. Detect support once per call via the signature
    # instead of blindly passing it (adapt, don't exclude - principle 9).

    @staticmethod
    def _supports_kwarg(fn, name: str) -> bool:
        try:
            return name in inspect.signature(fn).parameters
        except (TypeError, ValueError):  # builtins/exotic callables
            return False

    def _call_plan(self, text, overview, details, **kw):
        if not self._supports_kwarg(self.brain.plan, "history"):
            kw.pop("history", None)
        return self.brain.plan(text, overview, details, **kw)

    def _handle_user_prompt(self, params: dict) -> dict:
        """
        The user typed a natural-language request. Orchestrate the full loop:
          perceive (ask the add-on for the document overview)
            -> decompose the request into features (ADR 0017)
            -> 2+ features: agentic loop, one feature at a time
               (plan -> execute in its own undo group -> perceive -> next/replan)
            -> 0-1 features: the classic flat flow (v0.12 behaviour, unchanged):
               think -> execute each action -> bounded self-correction.
        Emits agent.status notifications throughout. Runs on a pool thread, so the
        nested calls back to the add-on do not deadlock (ADR 0003).
        Degrades gracefully if the local model is unavailable (principle 9).
        """
        self._task_counter += 1
        task_id = f"nl-{self._task_counter:04d}"
        text = (params.get("text") or "").strip() if isinstance(params, dict) else ""
        if not text:
            return {"accepted": False, "task_id": task_id, "error": "empty request"}
        log(f"[{task_id}] user.prompt: {text!r}")

        # ADR 0018: per-run question state. The panel's "Ask me when unsure"
        # checkbox travels with each request; absent (older add-on) = ON, which
        # is harmless because an older add-on cannot show questions anyway and
        # the engine degrades to silent defaults + log.
        self._ask_enabled = bool(params.get("ask_when_unsure", True)) \
            if isinstance(params, dict) else True
        self._questions_asked = 0

        # Optional per-request AI timeout from the panel (Phase 4). Default is
        # UNLIMITED; the user may opt into a cap. We apply whatever the panel sends
        # (including 0 = unlimited) so toggling the limit off resets a previous cap.
        if isinstance(params, dict) and "ai_timeout" in params \
                and hasattr(self.brain, "set_timeout"):
            ai_timeout = params.get("ai_timeout")
            if self.brain.set_timeout(ai_timeout):
                shown = "unlimited" if not ai_timeout else f"{ai_timeout}s"
                log(f"[{task_id}] AI per-call timeout set to {shown} (from panel)")

        # 0) Is the local model reachable? If not, try a lazy auto-start ONCE
        #    (Phase 4, ADR 0007), then re-check; if still down, refuse politely
        #    (principle 9). This covers "Ollama was killed after the engine
        #    started": the user does not have to restart the engine.
        avail = self.brain.availability()
        if not avail.get("available") and not self._ollama_autostart_tried \
                and hasattr(self.brain, "ensure_server"):
            self._ollama_autostart_tried = True
            self._notify(task_id, "starting-ai",
                         "local AI was off - trying to start it for you...",
                         privacy="local")
            outcome = self.brain.ensure_server(log=lambda m: log(f"[{task_id}] {m}"))
            log(f"[{task_id}] auto-start: {outcome.get('status')} - {outcome.get('message')}")
            avail = self.brain.availability()  # re-probe after the attempt
        if not avail.get("available"):
            reason = avail.get("reason", "local AI model not available")
            # Make the limitation actionable: structured commands always work, and
            # natural language resumes by itself once Ollama is up (no restart).
            friendly = (f"{reason} | Natural language needs the local AI (Ollama). "
                        "Meanwhile the structured commands (expert mode) work "
                        "normally; natural language resumes automatically as soon "
                        "as Ollama is running - no need to restart the engine.")
            log(f"[{task_id}] AI unavailable: {reason}")
            self._notify(task_id, "unavailable", friendly, privacy="local")
            return {"accepted": False, "task_id": task_id, "error": friendly}

        # 1) Perceive the active document (the agent's eyes).
        overview = self._perceive(task_id)

        # 1b) Feature decomposition (ADR 0017): a tiny extra inference that splits
        #     the request into features. 0-1 features (or any failure) => the
        #     classic v0.12 flat flow below, byte for byte (retro-compatibility;
        #     graceful degradation, principle 9).
        features = self._decompose(task_id, text, overview)

        # Checkpoint (ADR 0008): the user may have cancelled while we perceived
        # or while the model was decomposing.
        if self._is_cancelled(task_id):
            return self._cancelled_result(task_id)

        if len(features) > 1:
            return self._run_agentic(task_id, text, features, avail, overview)

        # ---- FLAT FLOW (v0.12 behaviour, unchanged) ---------------------------

        # 1c) Geometric RAG (Phase 3): for a small document, fetch the close-up of
        #     each object so the model can reference real edges/faces.
        details = self._gather_details(task_id, overview)

        # 2) Think: ask the local model for a plan.
        model_name = avail.get("model", "local model")
        self._notify(task_id, "thinking", f"asking the local model ({model_name})", privacy="local")
        try:
            plan = self._call_plan(text, overview, details,
                                   history=list(self._history))
        except OllamaUnavailable as exc:
            self._notify(task_id, "unavailable", str(exc), privacy="local")
            return {"accepted": False, "task_id": task_id, "error": str(exc)}
        except PlanError as exc:
            self._notify(task_id, "error", str(exc), privacy="local")
            return {"accepted": False, "task_id": task_id, "error": str(exc)}

        for note in plan.get("notes", []):
            self._notify(task_id, "note", note)

        # ADR 0019: the model itself may have asked ONE clarification. Settle
        # it (ask the user, then replan with the answer folded into the text).
        plan, text, ask_cancelled = self._settle_model_ask(
            task_id, text, plan,
            replan=lambda t: self._call_plan(t, overview, details,
                                             history=list(self._history)))
        if ask_cancelled:
            return self._cancelled_result(task_id)

        # ADR 0018: resolve fixable actions (missing params / bad enums) by
        # asking the user templated questions, or by declared defaults.
        valid_actions, q_cancelled = self._resolve_questions(task_id, plan)
        if q_cancelled:
            return self._cancelled_result(task_id)
        clarification = plan.get("clarification")
        if not valid_actions:
            msg = clarification or "the model did not produce any runnable action."
            self._notify(task_id, "clarification", msg)
            return {"accepted": True, "task_id": task_id, "results": [],
                    "clarification": msg}

        # Checkpoint (ADR 0008): if the user cancelled while the model was
        # thinking, discard the plan and run NOTHING on FreeCAD. This is the most
        # valuable cancel point: the slow step is the inference, and we stop right
        # after it without touching the document.
        if self._is_cancelled(task_id):
            return self._cancelled_result(task_id)

        # 3) Execute the plan (shared loop: bounded repair + id-chaining links).
        state = _RunState(self._known_ids(overview))
        cancelled = self._execute_actions(task_id, text, valid_actions,
                                          overview, details, state)
        if cancelled:
            return self._cancelled_result(task_id, state.results)

        results = state.results
        ok_count = sum(1 for r in results if r.get("ok"))
        self._notify(task_id, "completed",
                     f"{ok_count}/{len(results)} action(s) done (Ctrl+Z to undo)")
        return {"accepted": True, "task_id": task_id, "results": results,
                "summary": f"{ok_count}/{len(results)} action(s) executed"}

    # -- shared action-execution loop (extracted for ADR 0017) ------------------

    def _execute_actions(self, task_id: str, text: str, actions: list,
                         overview, details, state: "_RunState") -> bool:
        """
        Run a list of planned actions with BOUNDED self-correction on failure
        (principle 8: retry up to MAX_REPAIR_ATTEMPTS, never repeating an action
        we already tried) and the id-chaining links (ADR 0010/0013/0016).

        Results and chaining state accumulate in `state`, so consecutive batches
        (one per feature in the agentic loop, ADR 0017) chain onto each other
        exactly like consecutive steps of one flat plan.

        Returns True if the task was CANCELLED mid-way (the caller builds the
        cancelled result from state.results), False otherwise.
        """
        for idx, action in enumerate(actions, 1):
            # Checkpoint before EACH action: stop between steps of a multi-step
            # plan, leaving the already-applied steps in place (Ctrl+Z undoes them).
            if self._is_cancelled(task_id):
                return True
            # Within a plan, make an extrude consume the sketch the preceding
            # create_sketch / sketch_on_face just made (its REAL id), instead of
            # trusting the model to predict the auto-generated name like 'Sketch001'
            # (ADR 0010; same philosophy as ADR 0006 for edges).
            action = self._link_profile_target(task_id, action, state.last_profile_id)
            # Multi-profile chaining for loft/sweep (ADR 0021).
            action = self._link_profiles(task_id, action, state)
            # General id-chaining (ADR 0013), only when unambiguous (one creation).
            if state.created_count == 1:
                action = self._link_last_created(task_id, action,
                                                 state.last_created_id,
                                                 state.known_ids)
            # Consumed-object redirection (ADR 0016).
            action = self._link_consumed(task_id, idx, action, state.replaced)
            res = self._run_action(task_id, idx, action)
            tried = {self._action_signature(action)}
            attempts = 0
            current = action
            while not res.get("ok") and attempts < MAX_REPAIR_ATTEMPTS:
                if self._is_cancelled(task_id):
                    break  # stop self-correcting; record what we have so far.
                repaired = self.brain.repair(
                    text, current, str(res.get("error", "")), overview, details)
                if not repaired:
                    break
                repaired = self._link_profile_target(task_id, repaired,
                                                     state.last_profile_id)
                repaired = self._link_profiles(task_id, repaired, state)
                if state.created_count == 1:
                    repaired = self._link_last_created(task_id, repaired,
                                                       state.last_created_id,
                                                       state.known_ids)
                repaired = self._link_consumed(task_id, idx, repaired, state.replaced)
                sig = self._action_signature(repaired)
                if sig in tried:  # the model keeps proposing the same fix: stop.
                    self._notify(task_id, "repair",
                                 f"action {idx}: no new correction proposed, giving up")
                    break
                tried.add(sig)
                attempts += 1
                self._notify(task_id, "repair",
                             f"action {idx} failed; retry {attempts}/{MAX_REPAIR_ATTEMPTS} "
                             "with a corrected version")
                res = self._run_action(task_id, idx, repaired)
                current = repaired
            state.results.append(res)
            # Remember the sketch made in this plan so the next extrude consumes it;
            # an extrude clears it (the profile is now used up).
            state.last_profile_id = self._next_profile_id(
                state.last_profile_id, current, res)
            # Track the pool of unconsumed sketches for loft/sweep (ADR 0021).
            state.pending_profiles = self._update_pending(
                state.pending_profiles, current, res)
            # Remember the last object created and grow the set of known ids so the
            # next step can chain onto it (ADR 0013).
            state.last_created_id, state.known_ids = self._update_created(
                state.last_created_id, state.known_ids, res)
            # Note what the executed action consumed, so later steps that still
            # name the old id get redirected to its result (ADR 0016).
            state.replaced = self._record_consumed(current, res, state.replaced)
            if isinstance(res, dict) and res.get("ok") and res.get("created_ids"):
                state.created_count += 1
        return False

    # -- agentic per-feature loop (ADR 0017) ------------------------------------

    def _perceive(self, task_id: str):
        """Fetch perception.overview, best-effort (a failure degrades to None)."""
        self._notify(task_id, "perceiving", "looking at the active document",
                     privacy="local")
        try:
            return self.peer.call("perception.overview", {}, timeout=20)
        except (JsonRpcError, TimeoutError) as exc:
            self._notify(task_id, "warning", f"could not read the document: {exc}")
            return None

    def _decompose(self, task_id: str, text: str, overview) -> "list[str]":
        """
        Ask the brain to split the request into features (ADR 0017). Returns []
        for "no decomposition" (single-step request, stub brain without the
        method, or any failure) - the caller then uses the flat flow. Applies
        the MAX_FEATURES bound with a visible note (principle 8).
        """
        if not hasattr(self.brain, "decompose"):
            return []
        self._notify(task_id, "decomposing",
                     "checking whether the request splits into features",
                     privacy="local")
        try:
            if self._supports_kwarg(self.brain.decompose, "history"):
                features = self.brain.decompose(
                    text, overview, history=list(self._history)) or []
            else:
                features = self.brain.decompose(text, overview) or []
        except Exception:  # defensive: decompose() should never raise
            return []
        if len(features) > MAX_FEATURES:
            self._notify(task_id, "note",
                         f"request has {len(features)} features; running only the "
                         f"first {MAX_FEATURES} (engine bound)")
            features = features[:MAX_FEATURES]
        if len(features) > 1:
            listing = "; ".join(f"{i}. {f}" for i, f in enumerate(features, 1))
            self._notify(task_id, "decomposed",
                         f"{len(features)} feature(s): {listing}", privacy="local")
        return features

    def _tx_begin(self, task_id: str, label: str) -> bool:
        """
        Open a per-feature undo group on the add-on (ADR 0017): every FreeCAD
        transaction until transaction.end lands in ONE undo entry, so Ctrl+Z
        undoes the whole feature. Returns True if the group is open. On the
        first failure (older add-on / unsupported FreeCAD) we remember it and
        degrade to per-action transactions for the whole session (principle 9).
        """
        if not self._tx_supported:
            return False
        try:
            res = self.peer.call("transaction.begin", {"label": label}, timeout=20)
            if isinstance(res, dict) and res.get("ok"):
                return True
            reason = (res or {}).get("error", "unsupported")
        except (JsonRpcError, TimeoutError) as exc:
            reason = str(exc)
        self._tx_supported = False
        self._notify(task_id, "note",
                     f"per-feature undo not available ({reason}); falling back "
                     "to per-action undo")
        return False

    def _tx_end(self, task_id: str, abort: bool) -> None:
        """Close the current undo group (commit, or roll the feature back)."""
        try:
            self.peer.call("transaction.end", {"abort": bool(abort)}, timeout=20)
        except (JsonRpcError, TimeoutError) as exc:  # pragma: no cover - defensive
            self._notify(task_id, "warning",
                         f"could not close the undo group: {exc}")

    def _run_agentic(self, task_id: str, text: str, features: "list[str]",
                     avail: dict, overview) -> dict:
        """
        ADR 0017: the agentic per-feature loop. For each feature:
        perceive (fresh) -> plan ONLY that feature -> execute it inside one undo
        group -> on failure, roll the group back and replan once (bounded).
        Cancel (ADR 0008) is honoured at every checkpoint; a cancel mid-feature
        aborts the feature's group so no half-feature is left behind. If a
        feature still fails after its replan the RUN STOPS (later features
        usually depend on it; error cascades on a slow local model waste
        minutes) - the completed features remain, each undoable with Ctrl+Z.
        """
        model_name = avail.get("model", "local model")
        n = len(features)
        state = _RunState(self._known_ids(overview))
        for i, feature in enumerate(features, 1):
            if self._is_cancelled(task_id):
                return self._cancelled_result(task_id, state.results)
            self._notify(task_id, "feature", f"feature {i}/{n}: {feature}",
                         privacy="local")
            attempts = 0          # plan attempts for THIS feature (1 + replans)
            feature_ok = False
            last_error = ""
            while attempts <= MAX_REPLANS_PER_FEATURE and not feature_ok:
                feedback = last_error if attempts else None
                attempts += 1
                # (Re)perceive: earlier features / attempts changed the document.
                overview = self._perceive(task_id)
                details = self._gather_details(task_id, overview)
                state.known_ids |= self._known_ids(overview)
                if self._is_cancelled(task_id):
                    return self._cancelled_result(task_id, state.results)
                verb = "planning" if attempts == 1 else "replanning"
                self._notify(task_id, "thinking",
                             f"{verb} feature {i}/{n} ({model_name})",
                             privacy="local")
                try:
                    plan = self._call_plan(text, overview, details,
                                           feature=feature,
                                           done=state.done_features,
                                           feedback=feedback,
                                           history=list(self._history))
                except (OllamaUnavailable, PlanError) as exc:
                    last_error = str(exc)
                    self._notify(task_id, "error", last_error, privacy="local")
                    continue
                for note in plan.get("notes", []):
                    self._notify(task_id, "note", note)
                # ADR 0019: the model may ask ONE clarification per plan; the
                # answer is folded into the request text (it also helps the
                # remaining features).
                plan, text, ask_cancelled = self._settle_model_ask(
                    task_id, text, plan,
                    replan=lambda t, f=feature, fb=feedback: self._call_plan(
                        t, overview, details, feature=f,
                        done=state.done_features, feedback=fb,
                        history=list(self._history)))
                if ask_cancelled:
                    return self._cancelled_result(task_id, state.results)
                # ADR 0018: questions/defaults also inside the agentic loop
                # (the cap is per RUN, shared across features).
                valid, q_cancelled = self._resolve_questions(task_id, plan)
                if q_cancelled:
                    return self._cancelled_result(task_id, state.results)
                if not valid:
                    last_error = (plan.get("clarification")
                                  or "the model produced no runnable action "
                                     "for this feature")
                    self._notify(task_id, "clarification", last_error)
                    continue
                # Checkpoint: the user may have cancelled during the inference.
                if self._is_cancelled(task_id):
                    return self._cancelled_result(task_id, state.results)
                # Execute the feature inside ONE undo group (Ctrl+Z per feature).
                snap = state.snapshot()
                grouped = self._tx_begin(task_id, f"feature {i}/{n}: {feature}")
                cancelled = self._execute_actions(task_id, text, valid,
                                                  overview, details, state)
                batch = state.results[snap[-1]:]
                feature_ok = (not cancelled) and bool(batch) \
                    and all(isinstance(r, dict) and r.get("ok") for r in batch)
                if grouped:
                    # Commit the group on success; roll the WHOLE feature back on
                    # failure or cancel (atomic feature, no half-applied state).
                    self._tx_end(task_id, abort=not feature_ok)
                if cancelled:
                    if grouped:
                        # The batch was rolled back: report the run WITHOUT it.
                        state.restore(snap)
                    return self._cancelled_result(task_id, state.results)
                if not feature_ok:
                    failed = [r for r in batch
                              if not (isinstance(r, dict) and r.get("ok"))]
                    last_error = str(failed[0].get("error")) if failed \
                        else "the feature produced no result"
                    if grouped:
                        # Rolled back: restore the chaining state and replan.
                        state.restore(snap)
                        self._notify(task_id, "rollback",
                                     f"feature {i}/{n} failed and was rolled "
                                     f"back: {last_error}")
                    else:
                        # No group = no atomic rollback: replanning could apply
                        # the feature's good actions TWICE. Stop retrying this
                        # feature (degraded mode, principle 9).
                        self._notify(task_id, "error",
                                     f"feature {i}/{n} failed (no per-feature "
                                     f"rollback available): {last_error}")
                        break
            if not feature_ok:
                done = i - 1
                state.results.append({"ok": False, "transaction_id": "",
                                      "error": f"feature {i}/{n} failed: "
                                               f"{last_error}"})
                self._notify(task_id, "error",
                             f"feature {i}/{n} failed after {attempts} "
                             f"attempt(s): {last_error} - stopping the run; the "
                             f"{done} completed feature(s) remain (Ctrl+Z undoes "
                             "one feature at a time)")
                return {"accepted": True, "task_id": task_id,
                        "results": state.results,
                        "summary": f"stopped at feature {i}/{n}: {last_error}"}
            # One concise line per finished feature: this SUMMARY (not a
            # transcript) is what the next plan() sees - ctx 8192 realism.
            batch_created = [cid
                             for r in state.results[snap[-1]:]
                             if isinstance(r, dict) and r.get("ok")
                             for cid in (r.get("created_ids") or [])]
            line = f"feature {i}: {feature}"
            line += (f" -> created {', '.join(batch_created)}"
                     if batch_created else " -> done")
            state.done_features.append(line)
            self._notify(task_id, "feature-done",
                         f"feature {i}/{n} completed", privacy="local")
        ok_count = sum(1 for r in state.results
                       if isinstance(r, dict) and r.get("ok"))
        self._notify(task_id, "completed",
                     f"{n}/{n} feature(s) done ({ok_count} action(s); Ctrl+Z "
                     "undoes one feature at a time)")
        return {"accepted": True, "task_id": task_id, "results": state.results,
                "summary": f"{n} feature(s) completed "
                           f"({ok_count} action(s) executed)"}

    # -- consumed-target chaining (ADR 0016) -----------------------------------

    @staticmethod
    def _follow_replacements(ref: str, replaced: dict) -> str:
        """Resolve a reference through the chain of consumed->result mappings
        (Cylinder -> Drilled -> Drilled001 -> ...). Cycle-safe. Pure."""
        seen = set()
        while ref in replaced and ref not in seen:
            seen.add(ref)
            ref = replaced[ref]
        return ref

    @staticmethod
    def _rewrite_consumed(action: dict, replaced: dict) -> dict:
        """
        Return the action with references to CONSUMED objects redirected to the
        feature that swallowed them (ADR 0016). Within one plan, "drill Cylinder"
        after a previous drill already turned Cylinder into Drilled must target
        Drilled, or the second hole lands on the dead base and the visible result
        loses it. Returns the SAME object when nothing changes. Pure.
        """
        if not replaced or action.get("type", "command") != "command":
            return action
        params = action.get("params") or {}
        changed = {}
        for key in ("target", "a", "b", "profile", "path"):
            ref = params.get(key)
            if isinstance(ref, str) and ref:
                new = Session._follow_replacements(ref, replaced)
                if new != ref:
                    changed[key] = new
        # List-valued reference (loft.profiles, ADR 0021).
        refs = params.get("profiles")
        if isinstance(refs, list):
            new_refs = [Session._follow_replacements(r, replaced)
                        if isinstance(r, str) else r for r in refs]
            if new_refs != refs:
                changed["profiles"] = new_refs
        if not changed:
            return action
        new_params = dict(params)
        new_params.update(changed)
        out = dict(action)
        out["params"] = new_params
        return out

    @staticmethod
    def _record_consumed(action: dict, result: dict, replaced: dict) -> dict:
        """
        After a SUCCESSFUL action, record which referenced ids its new feature
        consumed (per CONSUMING_PARAMS), mapping old id -> first created id.
        Returns the updated mapping (input is not mutated). Pure.
        """
        if not (isinstance(result, dict) and result.get("ok")):
            return replaced
        created = result.get("created_ids") or []
        if not created:
            return replaced
        new_id = created[0]
        params = action.get("params") or {}
        out = dict(replaced)
        # (a) params whose referenced object the new feature consumes.
        for key in CONSUMING_PARAMS.get(action.get("cmd")) or ():
            ref = params.get(key)
            # A key may hold one id or a LIST of ids (loft.profiles, ADR 0021).
            refs = ref if isinstance(ref, list) else [ref]
            for r in refs:
                if isinstance(r, str) and r and r != new_id:
                    out[r] = new_id
        # (b) objects the EXECUTOR absorbed that are NOT params - reported in the
        # result as consumed_ids (e.g. a pocket's owner body). This closes the
        # ADR 0016 gap where "extrude op=cut" swallowed an owner not in params,
        # so a later feature naming the old body was not redirected.
        for r in result.get("consumed_ids") or ():
            if isinstance(r, str) and r and r != new_id:
                out[r] = new_id
        return out

    def _link_consumed(self, task_id: str, idx: int, action: dict,
                       replaced: dict) -> dict:
        """Apply _rewrite_consumed and log any redirection (transparency)."""
        new_action = self._rewrite_consumed(action, replaced)
        if new_action is not action:
            old_p = action.get("params") or {}
            new_p = new_action.get("params") or {}
            moves = ", ".join(f"{old_p[k]!r} -> {new_p[k]!r}"
                              for k in ("target", "a", "b", "profile",
                                        "path", "profiles")
                              if old_p.get(k) != new_p.get(k))
            self._notify(task_id, "linking",
                         f"action {idx}: reference(s) to a consumed object "
                         f"redirected: {moves}")
        return new_action

    @staticmethod
    def _detail_candidates(objs: "list | None") -> list:
        """
        Which objects deserve a geometric close-up in the prompt: the VISIBLE
        ones. Hidden objects are almost always consumed inputs (the base a Cut
        replaced, a drill tool, an extruded sketch): describing their edges and
        faces bloats the prompt of a small model and invites it to target dead
        geometry. The cheap overview still lists ALL ids, so the model can
        reference a hidden object when the user asks explicitly. (Session 13
        fix: a 3-object document pushed the prompt past the model context.)
        Pure helper, unit-tested headless.
        """
        return [o for o in (objs or [])
                if o.get("id") and o.get("visible") is not False]

    def _gather_details(self, task_id: str, overview: "dict | None") -> "list[dict]":
        """
        Phase 3 geometric RAG: fetch perception.detail for each existing object so
        the model can reference real Edge*/Face*. Best-effort: a failure here just
        falls back to the cheap overview (graceful degradation, principle 9).
        Only VISIBLE objects are inspected (see _detail_candidates).
        """
        if not overview:
            return []
        objs = self._detail_candidates(overview.get("objects"))
        if not objs or len(objs) > MAX_DETAIL_OBJECTS:
            return []  # empty or too big: stay concise, overview only.
        self._notify(task_id, "inspecting",
                     f"inspecting {len(objs)} object(s) for edges/faces", privacy="local")
        details: list = []
        for o in objs:
            oid = o.get("id")
            if not oid:
                continue
            try:
                d = self.peer.call("perception.detail", {"target": oid}, timeout=20)
                if isinstance(d, dict) and not d.get("error"):
                    details.append(d)
            except (JsonRpcError, TimeoutError):
                continue  # skip this one; the overview still covers it.
        return details

    @staticmethod
    def _action_signature(action: dict) -> str:
        """Stable signature of an action, to detect a repair that repeats itself."""
        try:
            import json as _json
            return _json.dumps(action, sort_keys=True)
        except Exception:
            return repr(action)

    # -- sketch -> extrude id chaining (ADR 0010) -------------------------------
    # A small model cannot predict the auto-generated name of a sketch it is about
    # to create (the first is 'Sketch', the next 'Sketch001', ...). So when a plan
    # does create_sketch then extrude, we rewrite the extrude's target to the id the
    # create_sketch actually produced, rather than the name the model guessed. This
    # is the same "resolve fragile references in the engine" idea as ADR 0006.

    @staticmethod
    def _is_cmd(action: dict, name: str) -> bool:
        return (isinstance(action, dict)
                and action.get("type", "command") == "command"
                and action.get("cmd") == name)

    @staticmethod
    def _rewrite_extrude_target(action: dict, profile_id):
        """
        Pure helper. If `action` is an extrude and `profile_id` is set and differs
        from the model's target, return (copy_with_new_target, True); otherwise
        return (action, False). Never mutates the input.
        """
        if profile_id and any(Session._is_cmd(action, c)
                              for c in PROFILE_CONSUMERS):
            params = dict(action.get("params") or {})
            if params.get("target") != profile_id:
                return {**action, "params": {**params, "target": profile_id}}, True
        return action, False

    @staticmethod
    def _next_profile_id(current_id, action: dict, res: dict):
        """
        Update the 'sketch created in this plan' marker after running an action: a
        successful create_sketch (or sketch_on_face) sets it to the created id; a
        successful extrude clears it (the profile is consumed); anything else leaves
        it unchanged.
        """
        if not isinstance(res, dict) or not res.get("ok"):
            return current_id
        if Session._is_cmd(action, "create_sketch") \
                or Session._is_cmd(action, "sketch_on_face"):
            ids = res.get("created_ids") or []
            return ids[0] if ids else current_id
        if any(Session._is_cmd(action, c)
               for c in PROFILE_CONSUMERS + LIST_PROFILE_CONSUMERS):
            return None  # the profile is consumed (extrude/revolve/loft/sweep)
        return current_id

    def _link_profile_target(self, task_id: str, action: dict, profile_id):
        """Apply _rewrite_extrude_target and log it transparently when it fires."""
        new_action, changed = self._rewrite_extrude_target(action, profile_id)
        if changed:
            self._notify(task_id, "linking",
                         f"extruding the sketch just created ({profile_id})",
                         privacy="local")
        return new_action

    # -- multi-profile chaining (ADR 0021) --------------------------------------
    # loft takes a LIST of profiles and sweep takes profile+path: the single
    # last_profile_id of ADR 0010 cannot serve them. The run-state tracks the
    # sketches created in this run and not yet consumed (pending_profiles, in
    # creation order); unknown profile references are rewritten to that pool,
    # oldest first (the model draws its profiles in the order it lofts them -
    # the few-shot examples enforce the convention).

    @staticmethod
    def _update_pending(pending: "list[str]", action: dict, res: dict) -> "list[str]":
        """After a SUCCESSFUL action, grow/shrink the unconsumed-sketch pool.
        Pure: returns a new list (or the input unchanged)."""
        if not (isinstance(res, dict) and res.get("ok")):
            return pending
        if Session._is_cmd(action, "create_sketch") \
                or Session._is_cmd(action, "sketch_on_face"):
            return list(pending) + list(res.get("created_ids") or [])
        params = (action.get("params") or {}) if isinstance(action, dict) else {}
        consumed: list = []
        if any(Session._is_cmd(action, c) for c in PROFILE_CONSUMERS):
            consumed = [params.get("target")]
        elif Session._is_cmd(action, "loft"):
            consumed = list(params.get("profiles") or [])
        elif Session._is_cmd(action, "sweep"):
            consumed = [params.get("profile"), params.get("path")]
        if not consumed:
            return pending
        return [p for p in pending if p not in consumed]

    @staticmethod
    def _rewrite_profile_lists(action: dict, pending: "list[str]",
                               known_ids: "set[str]"):
        """
        Pure helper. For a loft/sweep action, rewrite the profile references
        that do NOT resolve (not in known_ids) to the run's unconsumed sketches
        (pending, oldest first, each used once; entries already referenced are
        not reused). Returns (action_or_copy, [change descriptions]).
        """
        if not isinstance(action, dict) \
                or action.get("type", "command") != "command" \
                or action.get("cmd") not in LIST_PROFILE_CONSUMERS:
            return action, []
        params = dict(action.get("params") or {})
        changed: "list[str]" = []
        if action.get("cmd") == "loft":
            refs = params.get("profiles")
            if not isinstance(refs, list):
                return action, []
            avail = [p for p in pending if p not in refs]
            new_refs = []
            for r in refs:
                if isinstance(r, str) and r not in known_ids and avail:
                    sub = avail.pop(0)
                    changed.append(f"{r!r} -> {sub!r}")
                    new_refs.append(sub)
                else:
                    new_refs.append(r)
            if not changed:
                return action, []
            params["profiles"] = new_refs
        else:  # sweep
            current = (params.get("profile"), params.get("path"))
            avail = [p for p in pending if p not in current]
            for key in ("profile", "path"):
                r = params.get(key)
                if isinstance(r, str) and r not in known_ids and avail:
                    sub = avail.pop(0)
                    changed.append(f"{key}: {r!r} -> {sub!r}")
                    params[key] = sub
            if not changed:
                return action, []
        return {**action, "params": params}, changed

    def _link_profiles(self, task_id: str, action: dict,
                       state: "_RunState") -> dict:
        """Apply _rewrite_profile_lists and log it transparently when it fires."""
        new_action, changed = self._rewrite_profile_lists(
            action, state.pending_profiles, state.known_ids)
        if changed:
            self._notify(task_id, "linking",
                         "using the sketch(es) just created for "
                         f"{action.get('cmd')}: {', '.join(changed)}",
                         privacy="local")
        return new_action

    # -- generalized id chaining (ADR 0013) ------------------------------------
    # ADR 0010 solved one case (sketch -> extrude). The same fragility appears
    # whenever a plan says "create X, then operate on X": the model writes the name
    # it expects ("Box"), but FreeCAD may have auto-renamed it ("Box001"), or the
    # model uses a vague placeholder. We resolve it in the engine: track the last
    # object the plan created and, for a later step whose object reference does NOT
    # exist (neither in the document at plan start nor among the ids created so
    # far), rewrite that single reference to the last created id. Pure + logged.

    # For each command, the params that hold a reference to an EXISTING object.
    _REF_FIELDS = {
        "extrude": ("target",),
        "revolve": ("target",),
        "shell": ("target",),
        "sweep": ("profile", "path"),
        "drill_hole": ("target",),
        "fillet": ("target",),
        "chamfer": ("target",),
        "move": ("target",),
        "rotate": ("target",),
        "mirror": ("target",),
        "array": ("target",),
        "sketch_on_face": ("target",),
        "boolean": ("a", "b"),
    }

    @staticmethod
    def _known_ids(overview) -> "set[str]":
        """Ids the model could legitimately reference: the document at plan start."""
        ids: set = set()
        if isinstance(overview, dict):
            for o in overview.get("objects", []) or []:
                oid = o.get("id") if isinstance(o, dict) else None
                if oid:
                    ids.add(oid)
        return ids

    @staticmethod
    def _update_created(last_created_id, known_ids: "set[str]", res: dict):
        """After a successful action, remember its first created id and add the
        created ids to the known set (so the next step can reference them)."""
        if isinstance(res, dict) and res.get("ok"):
            ids = res.get("created_ids") or []
            if ids:
                return ids[0], (known_ids | set(ids))
        return last_created_id, known_ids

    @staticmethod
    def _rewrite_refs(action: dict, last_created_id, known_ids: "set[str]"):
        """
        Pure helper. If `action` references an object that is not known yet (not in
        `known_ids`) and exactly ONE such reference is unresolved, rewrite it to
        `last_created_id`. Returns (action_or_copy, [changed_field_names]); never
        mutates the input. Requiring a single unresolved reference avoids guessing
        for a boolean whose two operands are both unknown (too ambiguous to fix).
        """
        if not last_created_id or not isinstance(action, dict):
            return action, []
        if action.get("type", "command") != "command":
            return action, []
        fields = Session._REF_FIELDS.get(action.get("cmd"))
        if not fields:
            return action, []
        params = dict(action.get("params") or {})
        unresolved = [f for f in fields
                      if isinstance(params.get(f), str)
                      and params.get(f) not in known_ids]
        if len(unresolved) != 1:
            return action, []
        field = unresolved[0]
        if params.get(field) == last_created_id:
            return action, []
        params[field] = last_created_id
        return {**action, "params": params}, [field]

    def _link_last_created(self, task_id: str, action: dict,
                           last_created_id, known_ids: "set[str]"):
        """Apply _rewrite_refs and log it transparently when it fires."""
        new_action, changed = self._rewrite_refs(action, last_created_id, known_ids)
        if changed:
            self._notify(task_id, "linking",
                         f"using the object just created ({last_created_id}) "
                         f"for {', '.join(changed)}", privacy="local")
        return new_action

    def _run_action(self, task_id: str, idx: int, action: dict) -> dict:
        """Execute one planned action on the add-on. Returns a commandResult."""
        atype = action.get("type", "command")
        if atype == "python":
            reason = action.get("reason", "")
            self._notify(task_id, "python",
                         f"proposing free Python: {reason}", privacy="local")
            try:
                return self.peer.call("python.execute",
                                      {"code": action.get("code", ""), "reason": reason},
                                      timeout=120)
            except (JsonRpcError, TimeoutError) as exc:
                return {"ok": False, "transaction_id": "", "error": str(exc)}
        cmd = action.get("cmd")
        self._notify(task_id, "executing", f"action {idx}: {cmd}", privacy="local")
        try:
            return self.peer.call("command.execute",
                                  {"cmd": cmd, "params": action.get("params", {})},
                                  timeout=60)
        except (JsonRpcError, TimeoutError) as exc:
            return {"ok": False, "transaction_id": "", "error": str(exc)}

    def on_user_cancel(self, params: dict) -> dict:
        """
        Cooperative cancellation (ADR 0008). Record the task id; the running
        on_user_prompt loop checks it at the next checkpoint and stops there,
        WITHOUT executing any further FreeCAD action. Returns immediately (the
        UI stays responsive); the actual stop is reported via agent.status
        ("cancelling" now, "cancelled" when the loop reaches a checkpoint).
        We cannot interrupt an in-flight model inference, so a long "thinking"
        step finishes first and its plan is then discarded (nothing is run).
        """
        task_id = (params.get("task_id") or "").strip() if isinstance(params, dict) else ""
        if not task_id:
            return {"ok": False, "error": "user.cancel needs a task_id"}
        self._mark_cancelled(task_id)
        log(f"[{task_id}] user.cancel received - will stop at the next checkpoint")
        self._notify(task_id, "cancelling",
                     "cancel requested - stopping at the next safe point "
                     "(no further action will run)")
        return {"ok": True}

    # -- deterministic questions (ADR 0018) -------------------------------------

    def on_user_answer(self, params: dict) -> dict:
        """
        The add-on delivers the user's answer to a pending question (ADR 0018).
        {question_id, value?, use_default?}. Returns immediately; the waiting
        prompt loop wakes up at its next poll slice. An unknown/expired id is
        reported but harmless (e.g. an answer that arrived after the timeout).
        """
        qid = (params.get("question_id") or "").strip() \
            if isinstance(params, dict) else ""
        if not qid:
            return {"ok": False, "error": "user.answer needs a question_id"}
        with self._question_lock:
            event = self._answer_events.get(qid)
            if event is None:
                return {"ok": False, "error": f"unknown or expired question "
                                              f"'{qid}'"}
            self._answers[qid] = {"value": params.get("value"),
                                  "use_default": bool(params.get("use_default"))}
            event.set()
        return {"ok": True}

    def _next_question_id(self) -> str:
        with self._question_lock:
            self._question_counter += 1
            qid = f"q-{self._question_counter:04d}"
            self._answer_events[qid] = threading.Event()
            return qid

    def _drop_question(self, qid: str) -> None:
        with self._question_lock:
            self._answer_events.pop(qid, None)
            self._answers.pop(qid, None)

    def _ask_user(self, task_id: str, question: dict):
        """
        Ask ONE templated question through the add-on and wait for the answer.
        The wait is a cancellable checkpoint (ADR 0008 style): it polls in small
        slices so user.cancel takes effect quickly, and it gives up on the
        safety timeout, proceeding with the default (declared in the log).

        Returns ("answered", value) | ("default", reason) | ("cancelled", None).
        With an older add-on (user.question unknown) it degrades to silent
        defaults + log for the whole session (protocol 0.2.0 is additive).
        """
        default = question.get("default")
        if not self._questions_supported:
            return "default", "the add-on cannot show questions"
        qid = self._next_question_id()
        payload = dict(question)
        payload["question_id"] = qid
        payload["task_id"] = task_id
        try:
            res = self.peer.call("user.question", payload, timeout=20)
            if not (isinstance(res, dict) and res.get("ok")):
                raise JsonRpcError(ErrorCode.INTERNAL_ERROR,
                                   str((res or {}).get("error", "rejected")))
        except (JsonRpcError, TimeoutError) as exc:
            self._drop_question(qid)
            self._questions_supported = False
            return "default", f"the add-on cannot show questions ({exc})"
        self._notify(task_id, "question",
                     f"{question.get('question')} (waiting for your answer - "
                     "Cancel stops the run)")
        event = self._answer_events.get(qid)
        waited = 0.0
        try:
            while waited < QUESTION_WAIT_TIMEOUT:
                if event.wait(QUESTION_POLL_SLICE):
                    answer = self._answers.get(qid) or {}
                    if answer.get("use_default"):
                        return "default", "you chose the default"
                    return "answered", answer.get("value")
                waited += QUESTION_POLL_SLICE
                if self._is_cancelled(task_id):
                    return "cancelled", None
            return "default", (f"no answer within {int(QUESTION_WAIT_TIMEOUT)}s")
        finally:
            self._drop_question(qid)

    def _settle_model_ask(self, task_id: str, text: str, plan: dict, replan):
        """
        Handle the model's own clarification question (ADR 0019, level 2).
        Bounded loop: while the plan carries an "ask" (and the per-run question
        budget allows), ask the user through the SAME user.question channel as
        ADR 0018, fold the answer into the request text as
        "(Clarified: Q -> A)" and replan. The folded text also reaches the
        remaining features of an agentic run and the repair prompts - no new
        plumbing, and any brain (even one that ignores 'ask') keeps working.

        Golden rule enforced HERE: an ask without a default is never a blocking
        wait - it degrades to a plain clarification message (v0.12-style).
        With "Ask me when unsure" OFF the ask is ignored the same way.

        Returns (plan, text, cancelled).

        Sess.19 collaudo refinement: the DETERMINISTIC layer (ADR 0018) resolves
        a missing value ROBUSTLY - the user's numeric answer is applied directly,
        no fragile 4B replan. So when the plan ALSO carries a fixable action, we
        prefer that path and DROP the model's own (vaguer) ask. This fixes the
        observed failure where the model asked "what size?", the user typed 8,
        and the value was lost in the replan (diameter fell back to the default).
        """
        if self._ask_enabled and isinstance(plan, dict) \
                and isinstance(plan.get("ask"), dict):
            ordered = plan.get("ordered")
            if isinstance(ordered, list) and any(
                    isinstance(e, dict) and e.get("status") == "fixable"
                    for e in ordered):
                self._notify(task_id, "note",
                             "a precise templated question covers this - "
                             "using it instead of the model's own ask")
                plan = dict(plan)
                plan["ask"] = None
                return plan, text, False

        rounds = 0
        while True:
            ask = plan.get("ask") if isinstance(plan, dict) else None
            if not isinstance(ask, dict):
                return plan, text, False
            q = str(ask.get("question") or "").strip()
            default = ask.get("default")
            if not q or rounds >= MAX_QUESTIONS_PER_RUN:
                return plan, text, False
            if not self._ask_enabled or default is None:
                # No questions allowed / no default to offer: surface it as a
                # plain clarification when the model produced nothing runnable.
                if default is None and self._ask_enabled:
                    self._notify(task_id, "note",
                                 f"the model asked '{q}' without proposing a "
                                 "default; treating it as a clarification")
                if not plan.get("valid_actions"):
                    plan = dict(plan)
                    plan["clarification"] = plan.get("clarification") or q
                return plan, text, False
            rounds += 1
            if self._questions_asked >= MAX_QUESTIONS_PER_RUN:
                answer = default
                self._notify(task_id, "note",
                             f"question cap reached ({MAX_QUESTIONS_PER_RUN}"
                             f"/run): assuming '{q}' -> {default}")
            else:
                self._questions_asked += 1
                payload = {"cmd": "", "param": "", "question": q,
                           "options": ([str(o) for o in ask.get("options") or []]
                                       or [str(default)]),
                           "default": default}
                outcome, value = self._ask_user(task_id, payload)
                if outcome == "cancelled":
                    return plan, text, True
                if outcome == "answered":
                    answer = value
                    self._notify(task_id, "answer", f"'{q}' -> {answer}")
                else:
                    answer = default
                    self._notify(task_id, "note",
                                 f"assuming '{q}' -> {default} ({value})")
            text = f"{text}\n(Clarified: {q} -> {answer})"
            self._notify(task_id, "thinking",
                         "replanning with your clarification", privacy="local")
            try:
                plan = replan(text)
            except (OllamaUnavailable, PlanError) as exc:
                self._notify(task_id, "error",
                             f"replanning after the clarification failed: {exc}")
                plan = dict(plan)
                plan["ask"] = None
                return plan, text, False
            for note in plan.get("notes", []):
                self._notify(task_id, "note", note)

    def _resolve_questions(self, task_id: str, plan: dict):
        """
        Turn a plan into the final runnable action list (ADR 0018): walk the
        ordered entries; keep the valid ones; for each FIXABLE one (missing
        required params with defaults / invalid enum values) fill the gaps by
        asking the user - or by silently applying the declared default when
        asking is off-cap, unsupported or timed out - then re-validate.

        With "Ask me when unsure" OFF the v0.12 behaviour is reproduced
        EXACTLY: fixable actions are dropped with the classic note, no default
        is applied, no question is sent.

        Returns (actions, cancelled).
        """
        ordered = plan.get("ordered")
        if not isinstance(ordered, list):
            # Stub brain without the ordered view: nothing to resolve.
            return list(plan.get("valid_actions") or []), False

        out: list = []
        for entry in ordered:
            status = entry.get("status")
            action = entry.get("action") or {}
            if status == "valid":
                out.append(action)
                continue
            if status != "fixable":
                continue
            cmd = action.get("cmd")
            if not self._ask_enabled:
                # v0.12 identical: drop with the validator's own message.
                self._notify(task_id, "note",
                             f"action ({cmd}): dropped, "
                             f"{'; '.join(entry.get('errors') or [])}")
                continue
            params = dict(action.get("params") or {})
            gaps = [(p, None) for p in entry.get("missing") or []]
            gaps += [(p, params.get(p))
                     for p in (entry.get("bad_enum") or {})]
            cancelled = False
            for param, bad_value in gaps:
                question = questions_mod.build_question(
                    cmd, param, self.catalog, bad_value=bad_value)
                if question is None:  # defensive: classify() guarantees one
                    continue
                default = question.get("default")
                if self._questions_asked >= MAX_QUESTIONS_PER_RUN:
                    self._notify(task_id, "note",
                                 f"question cap reached ({MAX_QUESTIONS_PER_RUN}"
                                 f"/run): assuming {cmd}.{param} = {default}")
                    params[param] = default
                    continue
                self._questions_asked += 1
                outcome, value = self._ask_user(task_id, question)
                if outcome == "cancelled":
                    cancelled = True
                    break
                if outcome == "answered":
                    params[param] = questions_mod.coerce_answer(
                        value, cmd, param, self.catalog)
                    self._notify(task_id, "answer",
                                 f"using {cmd}.{param} = {params[param]}")
                else:  # default (chosen, unsupported add-on, or timeout)
                    params[param] = default
                    self._notify(task_id, "note",
                                 f"assuming {cmd}.{param} = {default} "
                                 f"({value})")
            if cancelled:
                return out, True
            invocation = {"cmd": cmd, "params": params}
            errors = fake_brain.validate_invocation(invocation, self.catalog)
            if fake_brain.is_blocking(errors):
                self._notify(task_id, "note",
                             f"action ({cmd}): dropped, {'; '.join(errors)}")
                continue
            out.append({"type": "command", **invocation})
        return out, False

    # -- cooperative cancellation helpers (ADR 0008) ---------------------------

    def _mark_cancelled(self, task_id: str) -> None:
        with self._cancel_lock:
            self._cancelled_tasks.add(task_id)

    def _is_cancelled(self, task_id: str) -> bool:
        with self._cancel_lock:
            return task_id in self._cancelled_tasks

    def _cancelled_result(self, task_id: str, results: "list | None" = None) -> dict:
        """Build the response when a task was stopped on user request."""
        results = results or []
        done = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
        self._notify(task_id, "cancelled",
                     f"stopped on request - {done} action(s) already done "
                     "(Ctrl+Z to undo).")
        log(f"[{task_id}] cancelled by the user after {done} action(s)")
        return {"accepted": True, "task_id": task_id, "cancelled": True,
                "results": results,
                "summary": f"cancelled after {done} action(s)"}

    # -- helpers ---------------------------------------------------------------

    def _notify(self, task_id: str, phase: str, message: str, privacy: str = "local") -> None:
        """Status notification to the UI (best-effort: errors don't block)."""
        try:
            self.peer.notify("agent.status", {
                "task_id": task_id, "phase": phase, "message": message, "privacy": privacy,
            })
        except Exception:  # pragma: no cover - defensive
            pass


def _register_engine_handlers(peer: JsonRpcPeer, session: "Session",
                              include_hello: bool) -> None:
    """
    Register the engine's operational handlers on a peer. These are the SAME in
    both topologies (the bridge is symmetric, ADR 0001): the engine always exposes
    command.request / user.prompt / user.cancel. Only `session.hello` differs:
      - SERVER standalone mode: the engine validates the token -> include it here.
      - CLIENT mode (production): the ADD-ON validates the token, so the engine
        does NOT expose session.hello; it CALLS it instead (see run_as_client).
    """
    if include_hello:
        peer.register("session.hello", session.on_hello)
    peer.register("command.request", session.on_command_request)
    peer.register("user.prompt", session.on_user_prompt)
    peer.register("user.cancel", session.on_user_cancel)
    peer.register("user.answer", session.on_user_answer)   # ADR 0018


def _prepare_ai(brain: "Brain") -> None:
    """
    Transparent Ollama auto-start + reachability log (ADR 0007). Shared by both run
    modes so natural language works the same whether the engine was launched by the
    add-on (client) or by the .bat (server). Defensive about the brain interface so
    a stub brain (headless tests) is fine.
    """
    if hasattr(brain, "ensure_server"):
        autostart = brain.ensure_server(log=log)
        log(f"local AI (Ollama) auto-start: {autostart.get('status')} - {autostart.get('message')}")
    avail = brain.availability()
    if avail.get("available"):
        models = ", ".join(avail.get("models", [])) or "(none installed)"
        flag = "OK" if avail.get("has_default_model") else "default model NOT pulled"
        log(f"local AI (Ollama): reachable [{flag}]. model={avail.get('model')}; installed: {models}")
    else:
        log("local AI (Ollama): NOT reachable. Natural language will be refused "
            "gracefully; structured commands from the panel still work, and "
            "natural language resumes by itself once Ollama is up (no restart).")
        log(f"  -> {avail.get('reason')}")


def serve_connection(client_sock: socket.socket, token: str,
                     catalog: fake_brain.Catalog, brain: "Brain | None" = None) -> None:
    """Handle ONE add-on connection from the greeting until disconnection."""
    conn = FramedConnection(client_sock)
    # Default (inline) dispatcher: handlers run on the peer's internal pool, so
    # they can make nested calls without deadlocking (ADR 0003).
    peer = JsonRpcPeer(conn, name="engine", logger=log)
    session = Session(peer, token, catalog, brain=brain)

    _register_engine_handlers(peer, session, include_hello=True)
    peer.start()

    try:
        if not session.authenticated.wait(timeout=30):
            log("handshake not received in time: closing the connection.")
            return
        log("add-on attached. Ready to receive commands from the panel. "
            "(The add-on may disconnect and reconnect at will.)")
        # Stay alive until the add-on closes the connection.
        peer.wait_closed()
        log("add-on disconnected.")
    finally:
        peer.close()


def serve_forever(accept_timeout: float | None = None, oneshot: bool = False) -> int:
    """
    Start the server and serve connections PERSISTENTLY until interrupted
    (Ctrl+C). Returns an exit code.
    """
    catalog = fake_brain.Catalog()
    # One brain shared by all connections (it is stateless across requests).
    brain = Brain(catalog)
    token = discovery.generate_token()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((discovery.HOST, 0))  # ephemeral port
    srv.listen(1)
    port = srv.getsockname()[1]

    discovery.write(port, token, PROTOCOL_VERSION)
    version, names = fake_brain.summarize(catalog)
    log(f"listening on {discovery.HOST}:{port} (ephemeral port)")
    log(f"discovery file: {discovery.DEFAULT_FILE}")
    log(f"ephemeral token: {token[:8]}... (loopback only)")
    log(f"vocabulary v{version}: {', '.join(names)}")
    # Phase 4 (ADR 0007): transparent auto-start + reachability probe.
    _prepare_ai(brain)
    log("ENGINE READY. Leave this window open and use the panel in FreeCAD.")
    log("To stop the engine: Ctrl+C in this window.")

    # Accept timeout only if requested (for tests); otherwise wait forever.
    srv.settimeout(accept_timeout)

    try:
        while True:
            log("waiting for a connection from the add-on...")
            try:
                client_sock, addr = srv.accept()
            except socket.timeout:
                log("no connection within the timeout: exiting.")
                return 2
            log(f"connection from {addr}")
            try:
                serve_connection(client_sock, token, catalog, brain=brain)
            except Exception as exc:  # a blown-up session must not kill the engine
                log(f"session ended with error: {exc}")
            if oneshot:
                log("oneshot mode: one connection served, exiting.")
                return 0
            # back to the top of the loop: ready for a new connection
    finally:
        srv.close()
        discovery.remove()
        log("server closed, discovery file removed.")


def run_as_client(host: str, port: int, token: str,
                  brain: "Brain | None" = None,
                  connect_timeout: float = 10.0, connect_attempts: int = 40) -> int:
    """
    PRODUCTION topology (ADR 0015; ADR 0002 prod). The ADD-ON is the TCP server and
    has launched us with its host/port/token. We connect to it, greet it with the
    token via session.hello (the ADD-ON validates it - handshake roles swapped),
    then serve the add-on's requests until the connection closes (e.g. FreeCAD is
    closed or the panel disconnects), at which point we exit so no orphan is left.

    Returns an exit code (0 = clean end, 2 = could not connect / handshake failed).
    """
    catalog = fake_brain.Catalog()
    brain = brain or Brain(catalog)
    version, names = fake_brain.summarize(catalog)
    log(f"CLIENT mode: connecting to the add-on server at {host}:{port} ...")
    log(f"vocabulary v{version}: {', '.join(names)}")

    # The add-on should already be listening before it launches us, but retry a few
    # times to absorb any start-up race (principle 8: correctness over speed).
    sock = None
    last_exc = None
    for _ in range(max(1, connect_attempts)):
        try:
            sock = socket.create_connection((host, port), timeout=connect_timeout)
            break
        except OSError as exc:
            last_exc = exc
            time.sleep(0.25)
    if sock is None:
        log(f"could not connect to the add-on server: {last_exc}")
        return 2
    # Blocking reads on the persistent link; call timeouts are the peer's job.
    sock.settimeout(None)

    conn = FramedConnection(sock)
    peer = JsonRpcPeer(conn, name="engine", logger=log)
    session = Session(peer, token, catalog, brain=brain)
    # CLIENT mode: we do NOT expose session.hello (the add-on validates); we call it.
    _register_engine_handlers(peer, session, include_hello=False)
    peer.start()

    # Transparent Ollama auto-start + reachability probe (same as server mode).
    _prepare_ai(brain)

    try:
        hello = peer.call("session.hello", {
            "token": token,
            "engine_version": ENGINE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
        }, timeout=connect_timeout)
    except (JsonRpcError, TimeoutError, ConnectionClosed) as exc:
        log(f"handshake with the add-on failed: {exc}")
        peer.close()
        return 2
    if not (isinstance(hello, dict) and hello.get("ok")):
        log(f"add-on rejected the handshake: {hello}")
        peer.close()
        return 2
    log(f"handshake OK. add-on v{hello.get('addon_version')}, "
        f"protocol {hello.get('protocol_version')}")
    log("ENGINE READY (client mode). Serving the add-on until it disconnects.")

    try:
        peer.wait_closed()
    except KeyboardInterrupt:
        pass
    finally:
        peer.close()
    log("add-on disconnected. Engine exiting (client mode).")
    return 0


def _connection_from_args_or_env(argv) -> "tuple[str, int, str] | None":
    """
    Return (host, port, token) if the add-on passed connection info (=> CLIENT
    mode), otherwise None (=> SERVER standalone mode for the .bat).

    CLI (any order): --host H --port P --token T  (also --host=H form).
    Env fallback: FREECAD_AGENT_HOST / FREECAD_AGENT_PORT / FREECAD_AGENT_TOKEN.
    """
    args: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            key, val = a[2:].split("=", 1)
            args[key] = val
            i += 1
        elif a.startswith("--") and i + 1 < len(argv):
            args[a[2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    host = args.get("host") or os.environ.get("FREECAD_AGENT_HOST")
    port = args.get("port") or os.environ.get("FREECAD_AGENT_PORT")
    token = args.get("token") or os.environ.get("FREECAD_AGENT_TOKEN")
    if host and port and token:
        try:
            return host, int(port), token
        except (TypeError, ValueError):
            return None
    return None


def main() -> int:
    log(f"FreeCAD Agent - engine v{ENGINE_VERSION}, protocol {PROTOCOL_VERSION}")
    conn_info = _connection_from_args_or_env(sys.argv[1:])
    if conn_info is not None:
        host, port, token = conn_info
        try:
            return run_as_client(host, port, token)
        except KeyboardInterrupt:
            log("interrupted by the user (Ctrl+C).")
            return 130
    # No connection info: SERVER standalone mode (discovery file) for .bat debug.
    log("no connection args: starting in SERVER standalone mode (debug / .bat).")
    raw_timeout = os.environ.get("FREECAD_AGENT_ACCEPT_TIMEOUT", "")
    accept_timeout = float(raw_timeout) if raw_timeout else None
    oneshot = os.environ.get("FREECAD_AGENT_ONESHOT", "") == "1"
    try:
        return serve_forever(accept_timeout=accept_timeout, oneshot=oneshot)
    except KeyboardInterrupt:
        log("interrupted by the user (Ctrl+C).")
        discovery.remove()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
