"""
engine/brain.py - the REAL planning brain (Phase 2).

It replaces the "decision" role of the Phase 1 fake brain: given a natural-language
request (and a concise perception of the active document), it asks a local model
(via Ollama) to produce a PLAN: an ordered list of actions. Each action is either

  - a structured-vocabulary command  {type:"command", cmd, params}, or
  - a free-Python proposal           {type:"python", code, reason}.

Design choices (see ADR 0004):
  - Model-agnostic structured output: we describe the exact JSON shape in the
    system prompt and force JSON mode (ollama format="json"). We do NOT rely on a
    specific model's native tool-calling, so the engine "adapts, not excludes"
    (principle 9). The tester's model is qwen3:4b, but nothing here is tuned to it.
  - The vocabulary the model sees is GENERATED from shared/commands.schema.json
    (principle 5: the vocabulary is neutral data). Add a command to the schema and
    the brain automatically offers it to the model.
  - Validation downstream of the model (principle 7: don't trust, verify): every
    command action is re-validated against the catalog by fake_brain before it is
    allowed to run. Invalid actions are dropped with a reported reason.
  - Self-correction (principle 6/8): repair() lets the engine feed an execution
    error back to the model and ask for a corrected action.

This module has NO dependency on FreeCAD and is fully testable headless by
injecting a fake "chat" function in place of the Ollama client.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import fake_brain  # engine/fake_brain.py: catalog loader + validator (stdlib)
import questions   # engine/questions.py: templated questions (ADR 0018)
from ollama_client import OllamaClient, OllamaUnavailable

# A "chat" callable: (system_prompt, user_prompt) -> parsed JSON dict.
# Default is the real Ollama client; tests inject a fake one.
ChatFn = Callable[[str, str], Dict[str, Any]]


class PlanError(RuntimeError):
    """The brain could not produce a usable plan (model error or unparseable)."""


class Brain:
    """Turns natural language into a validated plan of CAD actions."""

    def __init__(self, catalog: Optional[fake_brain.Catalog] = None,
                 chat: Optional[ChatFn] = None,
                 client: Optional[OllamaClient] = None) -> None:
        self.catalog = catalog or fake_brain.Catalog()
        # Either an explicit chat function (tests) or a real Ollama client.
        self._client = client or OllamaClient()
        self._chat: ChatFn = chat or self._client.chat_json

    # -- runtime configuration -------------------------------------------------

    def set_timeout(self, seconds) -> bool:
        """
        Set the per-call Ollama timeout at runtime (Phase 4: configurable from the
        panel instead of only via FREECAD_AGENT_OLLAMA_TIMEOUT).

        Convention: None or a non-positive number means UNLIMITED (no timeout) -
        the default; a positive number caps each model call. Best-effort and never
        raises: ignores bad values and clients without a `timeout` attribute (e.g. a
        fake chat injected in tests). Returns True if it was applied.
        """
        if not hasattr(self._client, "timeout"):
            return False
        if seconds is None:
            self._client.timeout = None  # unlimited
            return True
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return False
        # 0 or negative => unlimited; otherwise the positive cap.
        self._client.timeout = value if value > 0 else None
        return True

    def get_timeout(self):
        """Return the current per-call Ollama timeout, or None if not applicable."""
        return getattr(self._client, "timeout", None)

    # -- availability ----------------------------------------------------------

    def availability(self) -> Dict[str, Any]:
        """Report whether the local model is reachable (for graceful degradation)."""
        try:
            models = self._client.list_models()
        except OllamaUnavailable as exc:
            return {"available": False, "reason": str(exc), "models": []}
        except Exception:  # a fake chat without a real client: assume available
            return {"available": True, "reason": "", "models": []}
        if not models:
            return {
                "available": False,
                "reason": ("Ollama is running but no model is installed. "
                           f"Pull one, e.g. `ollama pull {self._client.model}`."),
                "models": [],
            }
        # Resolve the tag we will actually use (may differ from the configured one).
        try:
            effective = self._client.effective_model()
        except Exception:
            effective = self._client.model
        return {
            "available": True,
            "reason": "",
            "models": models,
            "has_default_model": self._client.has_model(),
            "model": effective,
        }

    # -- transparent auto-start (Phase 4, ADR 0007) ----------------------------

    def ensure_server(self, log=None, wait_seconds: float = 20.0) -> Dict[str, Any]:
        """
        Make sure the local AI server (Ollama) is running, launching it if needed.

        Delegates to ollama_launch.ensure_running using THIS brain's client as the
        reachability probe. Only meaningful with a real OllamaClient; with a fake
        chat injected for tests it is a harmless no-op probe. Never raises.
        """
        try:
            from ollama_launch import ensure_running
        except Exception as exc:  # pragma: no cover - import guard
            return {"status": "error", "launched": False, "message": str(exc)}
        # The client must expose a no-raise is_available(); the real OllamaClient
        # does. If a bare fake without it was injected, skip gracefully.
        if not callable(getattr(self._client, "is_available", None)):
            return {"status": "skipped", "launched": False,
                    "message": "no real Ollama client to probe."}
        return ensure_running(self._client, log=log, wait_seconds=wait_seconds)

    # -- planning --------------------------------------------------------------

    def plan(self, request: str, overview: Optional[dict] = None,
             details: Optional[List[dict]] = None,
             feature: Optional[str] = None,
             done: Optional[List[str]] = None,
             feedback: Optional[str] = None,
             history: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Produce a plan from a natural-language request.

        Args:
          request:  the user's natural-language text.
          overview: the cheap document overview (perception.overview): one line
                    per object (id/type/label).
          details:  optional list of objectDetail dicts (perception.detail), the
                    "geometric RAG" of Phase 3. They give the model the REAL
                    Edge*/Face* references (with hints) it needs to fillet, chamfer
                    or drill precise sub-elements, instead of guessing them.
          feature:  (ADR 0017) when set, ask the model to plan ONLY this feature
                    of the overall request (agentic per-feature loop).
          done:     (ADR 0017) one-line summaries of the features already built,
                    so the model does not redo them. A SUMMARY, not a transcript
                    (realism: small models, ctx 8192).
          feedback: (ADR 0017) failure text of the previous attempt at this same
                    feature, for the bounded replan.
          history:  (ADR 0019) compressed one-line summaries of this session's
                    EARLIER requests (sliding window), so the model can resolve
                    anaphora ("drill it") against objects made minutes ago.
                    Summaries, never transcripts (realism: ctx 8192).

        Returns: {"actions": [...], "valid_actions": [...], "notes": [...],
                  "clarification": str|None, "ask": dict|None}
        - ask: (ADR 0019) set when the model asked ONE clarification question
          {"question", "options", "default"} instead of (or besides) acting.
        - actions:        everything the model proposed (raw)
        - valid_actions:  the subset that passed validation and is safe to run
        - notes:          human-readable messages (dropped actions, warnings)
        - clarification:  set when the model asks for more info / refuses
        Raises PlanError if the model reply is unusable.
        """
        system = self._system_prompt()
        user = self._user_prompt(request, overview, details, feature, done,
                                 feedback, history)
        try:
            reply = self._chat(system, user)
        except OllamaUnavailable:
            raise
        except Exception as exc:  # parsing or transport problem
            raise PlanError(f"the model did not return a usable plan: {exc}") from exc

        return self._normalize(reply)

    def repair(self, request: str, failed_action: dict, error: str,
               overview: Optional[dict] = None,
               details: Optional[List[dict]] = None) -> Optional[dict]:
        """
        Ask the model to fix a single action that failed at execution time
        (self-correction). Returns a corrected action dict, or None if the model
        cannot fix it. Bounded by the caller to avoid loops (principle 8).

        The geometric detail (Phase 3) is fed back too: many failures are wrong
        Edge*/Face* references, which the detail lets the model correct.
        """
        system = self._system_prompt()
        user = (
            "A previous action FAILED when executed in FreeCAD. "
            "Return a corrected plan as the same JSON object with an 'actions' "
            "array (usually a single fixed action). Do not repeat the same mistake.\n\n"
            f"Original request: {request}\n"
            f"Failed action: {json.dumps(failed_action)}\n"
            f"FreeCAD error: {error}\n\n"
            f"{self._overview_block(overview)}"
            f"{self._details_block(details)}"
        )
        try:
            reply = self._chat(system, user)
        except Exception:
            return None
        plan = self._normalize(reply)
        valid = plan.get("valid_actions") or []
        return valid[0] if valid else None

    # -- prompt construction ---------------------------------------------------

    def _system_prompt(self) -> str:
        """Describe the role, the vocabulary (from the schema) and the output shape."""
        lines: List[str] = [
            "You are the planning brain of FreeCAD Agent, an assistant that builds "
            "3D CAD models in FreeCAD. Convert the user's request into an ordered "
            "plan of actions. Units are millimetres and degrees.",
            "",
            "You can use these STRUCTURED COMMANDS (prefer them whenever they fit):",
        ]
        lines.append(self._catalog_block())
        lines += [
            "",
            "If (and only if) no structured command fits, you may propose FREE "
            "PYTHON for FreeCAD as an action of type 'python' with fields 'code' "
            "(FreeCAD Python using the variables `doc` and `FreeCAD`) and 'reason' "
            "(why the vocabulary was not enough). The user always sees this code.",
            "",
            "To reference an EXISTING object, use the exact `id` from the document "
            "overview the user gives you. Do not invent ids.",
            "",
            "Answer with ONE JSON object, no prose, with this exact shape:",
            '{"actions": [',
            '  {"type": "command", "cmd": "<command name>", "params": { ... }},',
            '  {"type": "python", "code": "<freecad python>", "reason": "<why>"}',
            "],",
            '"clarification": "<set only if you cannot proceed and need more info, '
            'otherwise omit or null>"}',
            "",
            "Rules: output valid JSON only; keep the plan minimal and correct; "
            "never include comments in the JSON; if the request is impossible or "
            "ambiguous, return an empty actions array and a clarification message.",
            "",
            "If ONE crucial value is missing and a wrong guess would ruin the "
            "part, you may ask ONE short question instead of guessing: add "
            '"ask": {"question": "...", "options": ["..."], "default": <value>} '
            "to your JSON (ALWAYS include a sensible default). Never ask "
            "permission to act, only for missing information; when a sensible "
            "default is obvious, use it and act instead of asking.",
            "",
            "For fillet and chamfer you normally do NOT list edges. Omit 'edges' "
            "and optionally set 'where' to choose a group: 'all' (default), 'top', "
            "'bottom', 'vertical' or 'horizontal'. The tool reads the real geometry "
            "and selects the matching edges itself. Only pass an explicit 'edges' "
            "list (e.g. from 'DETAILED GEOMETRY') when the user clearly wants "
            "specific edges; never invent edge numbers. "
            "For drilling a centred hole you do NOT need a position: omit it and the "
            "tool drills from the top centre automatically. To drill at a specific "
            "point instead, pass position [x, y] (top-view coordinates in mm); the "
            "tool drills downwards from the top face at that point.",
            "",
            "To EXTRUDE a 2D shape into a solid, first create the profile with "
            "create_sketch (it makes an object named 'Sketch'), then call extrude "
            "with target 'Sketch'. A rectangle needs width and height; a circle "
            "needs radius. The default plane is XY. To make a SOLID OF REVOLUTION "
            "(vase, pulley, knob), create_sketch the profile then revolve it: "
            "angle defaults to 360 and axis is a letter 'X'/'Y'/'Z' (default 'Z'). "
            "For a LOFT draw each profile with create_sketch (space them with "
            "placement [x,y,z]) then loft with profiles [names in order]. For a "
            "SWEEP draw the cross-section PROFILE first, then the PATH (e.g. a "
            "polyline on a perpendicular plane), then sweep. Use shell to hollow "
            "a solid: give the wall thickness and pick the OPEN face with where "
            "'top'/'bottom'.",
            "",
            "To MOVE an existing object use move with 'by' [dx,dy,dz] for a "
            "relative shift (preferred) or 'to' [x,y,z] for an absolute position. "
            "To ROTATE an existing object use rotate with an 'angle' in degrees and "
            "an 'axis' of 'X', 'Y' or 'Z' (default 'Z'); the tool spins it around "
            "its own centre. Give axes as these letters, never as raw vectors.",
            "",
            "To DUPLICATE shapes: use mirror to reflect an object across a plane "
            "('XY'/'XZ'/'YZ', the original is kept); use array for a pattern of "
            "copies - pattern 'linear' (with count, spacing and a direction X/Y/Z) "
            "or pattern 'polar' (with count and axis X/Y/Z). count is the TOTAL "
            "number of items including the original. A POLAR pattern makes the items "
            "ORBIT a central axis (the file origin by default), forming a ring - it "
            "does NOT spin the object on itself. If the user names a centre or point, "
            "pass it as center [x,y,z]; if the user gives a circle radius, pass it as "
            "radius (the tool then places the items on that circle); otherwise the "
            "items orbit at the object's current distance from the centre. Optional "
            "angle (default a full 360 circle). To operate on something you JUST "
            "created, reference it by the name you gave it; the tool links to the "
            "real object automatically.",
            "",
            "To add or remove material on an existing body: use sketch_on_face to "
            "put a sketch on a flat face (where 'top' or 'bottom'; the tool finds "
            "the real face), then extrude it. extrude with op 'add' (default) grows "
            "a boss out of the face; extrude with op 'cut' sinks a POCKET into the "
            "body. Do not guess face numbers - use 'where'.",
            "",
            "EXAMPLES (input on the left, the exact JSON you must output on the right):",
            'Request: "create a box 30x20x10"',
            '{"actions": [{"type": "command", "cmd": "create_box", '
            '"params": {"length": 30, "width": 20, "height": 10}}]}',
            'Request: "drill a 6 mm hole through the centre of Box" '
            '(document has Box, height 10)',
            '{"actions": [{"type": "command", "cmd": "drill_hole", '
            '"params": {"target": "Box", "diameter": 6, "depth": 10}}]}',
            'Request: "drill a 5 mm hole 12 mm deep in Plate at position [30, 0]" '
            '(document has Plate)',
            '{"actions": [{"type": "command", "cmd": "drill_hole", '
            '"params": {"target": "Plate", "diameter": 5, "depth": 12, '
            '"position": [30, 0]}}]}',
            'Request: "round all the edges of Box with radius 2"',
            '{"actions": [{"type": "command", "cmd": "fillet", "params": '
            '{"target": "Box", "radius": 2}}]}',
            'Request: "chamfer the top edges of Box by 1.5"',
            '{"actions": [{"type": "command", "cmd": "chamfer", "params": '
            '{"target": "Box", "size": 1.5, "where": "top"}}]}',
            'Request: "merge Box and Cylinder"',
            '{"actions": [{"type": "command", "cmd": "boolean", "params": '
            '{"op": "union", "a": "Box", "b": "Cylinder"}}]}',
            'Request: "create a box 40x40x10 and drill a 8 mm hole in the centre"',
            '{"actions": [{"type": "command", "cmd": "create_box", "params": '
            '{"length": 40, "width": 40, "height": 10}}, '
            '{"type": "command", "cmd": "drill_hole", "params": '
            '{"target": "Box", "diameter": 8, "depth": 10}}]}',
            'Request: "draw a 40x30 rectangle and extrude it 10 mm"',
            '{"actions": [{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "rectangle", "width": 40, "height": 30}}, '
            '{"type": "command", "cmd": "extrude", "params": '
            '{"target": "Sketch", "distance": 10}}]}',
            'Request: "extrude a circle of radius 12 by 20 mm"',
            '{"actions": [{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "circle", "radius": 12}}, '
            '{"type": "command", "cmd": "extrude", "params": '
            '{"target": "Sketch", "distance": 20}}]}',
            'Request: "move Box 20 mm along X" (document has Box)',
            '{"actions": [{"type": "command", "cmd": "move", "params": '
            '{"target": "Box", "by": [20, 0, 0]}}]}',
            'Request: "rotate Cylinder 45 degrees around Z" (document has Cylinder)',
            '{"actions": [{"type": "command", "cmd": "rotate", "params": '
            '{"target": "Cylinder", "angle": 45, "axis": "Z"}}]}',
            'Request: "mirror Bracket across the YZ plane" (document has Bracket)',
            '{"actions": [{"type": "command", "cmd": "mirror", "params": '
            '{"target": "Bracket", "plane": "YZ"}}]}',
            'Request: "make a row of 5 copies of Box, 30 mm apart along X" '
            '(document has Box)',
            '{"actions": [{"type": "command", "cmd": "array", "params": '
            '{"target": "Box", "pattern": "linear", "count": 5, "spacing": 30, '
            '"direction": "X"}}]}',
            'Request: "arrange 6 copies of Pin in a circle around Z" '
            '(document has Pin)',
            '{"actions": [{"type": "command", "cmd": "array", "params": '
            '{"target": "Pin", "pattern": "polar", "count": 6, "axis": "Z"}}]}',
            'Request: "arrange 8 copies of Hole on a circle of radius 40 around the '
            'Z axis" (document has Hole)',
            '{"actions": [{"type": "command", "cmd": "array", "params": '
            '{"target": "Hole", "pattern": "polar", "count": 8, "axis": "Z", '
            '"radius": 40}}]}',
            'Request: "draw a 20x10 rectangle on the top face of Box and extrude it '
            '5 mm" (document has Box)',
            '{"actions": [{"type": "command", "cmd": "sketch_on_face", "params": '
            '{"target": "Box", "where": "top", "shape": "rectangle", "width": 20, '
            '"height": 10}}, {"type": "command", "cmd": "extrude", "params": '
            '{"target": "Sketch", "distance": 5}}]}',
            'Request: "cut a round pocket of radius 6, 4 mm deep, into the top of '
            'Box" (document has Box)',
            '{"actions": [{"type": "command", "cmd": "sketch_on_face", "params": '
            '{"target": "Box", "where": "top", "shape": "circle", "radius": 6}}, '
            '{"type": "command", "cmd": "extrude", "params": '
            '{"target": "Sketch", "distance": 4, "op": "cut"}}]}',
            'Request: "make a hexagonal prism, radius 15, 8 mm tall"',
            '{"actions": [{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "polygon", "sides": 6, "radius": 15}}, '
            '{"type": "command", "cmd": "extrude", "params": '
            '{"target": "Sketch", "distance": 8}}]}',
            'Request: "loft a 40x40 square into a circle of radius 10, 30 mm '
            'above it"',
            '{"actions": [{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "rectangle", "width": 40, "height": 40}}, '
            '{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "circle", "radius": 10, "placement": [0, 0, 30]}}, '
            '{"type": "command", "cmd": "loft", "params": '
            '{"profiles": ["Sketch", "Sketch001"]}}]}',
            'Request: "hollow out Box with 2 mm walls, open on top" '
            '(document has Box)',
            '{"actions": [{"type": "command", "cmd": "shell", "params": '
            '{"target": "Box", "thickness": 2, "where": "top"}}]}',
            'Request: "create a cone with base radius 10 and height 25"',
            '{"actions": [{"type": "command", "cmd": "create_cone", '
            '"params": {"radius1": 10, "height": 25}}]}',
            'Request: "draw a 20x10 rectangle and revolve it around the X axis"',
            '{"actions": [{"type": "command", "cmd": "create_sketch", "params": '
            '{"shape": "rectangle", "width": 20, "height": 10}}, '
            '{"type": "command", "cmd": "revolve", "params": '
            '{"target": "Sketch", "axis": "X"}}]}',
            'Request: "drill a mounting hole in Plate" (hole size unknown and '
            'it matters for mounting)',
            '{"actions": [], "ask": {"question": "What diameter should the '
            'mounting hole have?", "options": ["5", "6", "8"], "default": 6}}',
        ]
        return "\n".join(lines)

    def _catalog_block(self) -> str:
        """Compact, model-friendly description of each command and its parameters."""
        out: List[str] = []
        for name in self.catalog.names():
            spec = self.catalog.spec(name) or {}
            summary = spec.get("summary", "")
            pschema = spec.get("params", {})
            required = set(pschema.get("required", []))
            props = pschema.get("properties", {})
            parts = []
            for pname, pspec in props.items():
                ptype = pspec.get("type", "any")
                if "enum" in pspec:
                    ptype = "one of " + "/".join(map(str, pspec["enum"]))
                tag = "required" if pname in required else "optional"
                parts.append(f"{pname} ({ptype}, {tag})")
            params_desc = "; ".join(parts) if parts else "no parameters"
            out.append(f"- {name}: {summary} Params: {params_desc}.")
        return "\n".join(out)

    def _user_prompt(self, request: str, overview: Optional[dict],
                     details: Optional[List[dict]] = None,
                     feature: Optional[str] = None,
                     done: Optional[List[str]] = None,
                     feedback: Optional[str] = None,
                     history: Optional[List[str]] = None) -> str:
        head = (f"{self._history_block(history)}{self._overview_block(overview)}"
                f"{self._details_block(details)}")
        if not feature:
            return f"{head}User request: {request}"
        # ADR 0017: agentic per-feature loop. The run-state block is a concise
        # SUMMARY (one line per finished feature), never a transcript, so it
        # stays within a small model's context budget.
        lines = [f"{head}Overall user request: {request}", ""]
        if done:
            lines.append("Features already built (do NOT redo them):")
            lines.extend(f"  - {d}" for d in done)
            lines.append("")
        if feedback:
            lines.append(f"The previous attempt at this feature FAILED: {feedback}")
            lines.append("Plan it differently this time.")
            lines.append("")
        lines.append(f"Now plan ONLY this feature, nothing else: {feature}")
        return "\n".join(lines)

    # -- feature decomposition (ADR 0017) ---------------------------------------

    def decompose(self, request: str, overview: Optional[dict] = None,
                  history: Optional[List[str]] = None) -> List[str]:
        """
        Ask the model to split a request into an ordered list of FEATURES
        (ADR 0017, agentic loop). The prompt is deliberately TINY - no command
        catalog, no long examples - so this extra inference stays cheap on a
        small local model.

        Returns a list of feature strings, or [] when the request is a single
        operation, the model does not decompose, or anything at all fails: the
        caller then falls back to the classic single-plan flow (graceful
        degradation, principle 9). NEVER raises.
        """
        try:
            reply = self._chat(self._decompose_system_prompt(),
                               self._decompose_user_prompt(request, overview,
                                                           history))
        except Exception:
            return []
        feats = reply.get("features") if isinstance(reply, dict) else None
        if not isinstance(feats, list):
            return []
        out = [f.strip() for f in feats if isinstance(f, str) and f.strip()]
        # Fewer than 2 features = nothing to loop over: signal "flat flow".
        return out if len(out) >= 2 else []

    def _decompose_system_prompt(self) -> str:
        return "\n".join([
            "You break a CAD modelling request into an ordered list of FEATURES.",
            "A feature is one self-contained modelling step: a base solid, a set "
            "of holes, a pocket or boss, rounded/chamfered edges, a pattern of "
            "copies, a move/rotation.",
            "Keep every dimension and number from the request inside the feature "
            "that uses it. Order the features so each builds on the previous ones.",
            'Answer with ONE JSON object only: {"features": ["...", "..."]}',
            "If the request is ONE simple operation (or you are unsure), answer "
            '{"features": []} and it will be handled as a single step.',
            "",
            "Examples:",
            'Request: "make a 100x60x10 plate with 4 corner holes of 6 mm and '
            'rounded vertical edges"',
            '{"features": ["create the base plate 100x60x10", '
            '"drill 4 holes of 6 mm at the corners of the plate", '
            '"round the vertical edges of the plate"]}',
            'Request: "create a box 30x20x10"',
            '{"features": []}',
        ])

    @staticmethod
    def _decompose_user_prompt(request: str, overview: Optional[dict],
                               history: Optional[List[str]] = None) -> str:
        ids = []
        if isinstance(overview, dict):
            ids = [o.get("id") for o in overview.get("objects", []) or []
                   if isinstance(o, dict) and o.get("id")]
        ctx = f"Existing objects in the document: {', '.join(ids)}\n" if ids else ""
        # ADR 0019: a pinch of session memory (last lines only - tiny prompt).
        if history:
            recent = "; ".join(str(h)[:120] for h in history[-3:])
            ctx = f"Earlier in this session: {recent}\n{ctx}"
        return f"{ctx}Request: {request}"

    @staticmethod
    def _history_block(history: Optional[List[str]]) -> str:
        """
        Render the compressed session memory (ADR 0019): one line per EARLIER
        request of this session, oldest first, hard-capped in size (the engine
        already keeps a sliding window; this cap is defence in depth for the
        ctx-8192 budget). Nothing is rendered for the first request.
        """
        if not history:
            return ""
        lines = ["EARLIER IN THIS SESSION (oldest first; the document overview "
                 "below shows the CURRENT ids):"]
        budget = 1000  # chars, ~250 tokens: declared prompt budget of the memory
        used = 0
        for h in history:
            h = str(h)[:200]
            if used + len(h) > budget:
                break
            lines.append(f"  - {h}")
            used += len(h)
        lines.append("Pronouns like 'it' or 'that' refer to the objects made "
                     "above, unless the request says otherwise.")
        return "\n".join(lines) + "\n\n"

    def _details_block(self, details: Optional[List[dict]]) -> str:
        """
        Render the geometric RAG concisely (Phase 3). One short paragraph per
        object: dimensions, bounding box and the referenceable Edge*/Face* with
        their hints, so a small local model can target real sub-elements. Kept
        terse on purpose (context.schema.json goal: do not saturate small models).
        """
        if not details:
            return ""
        lines = ["DETAILED GEOMETRY (use these exact references):"]
        for d in details:
            if not isinstance(d, dict) or d.get("error"):
                continue
            oid = d.get("id", "?")
            otype = d.get("type", "?")
            dims = d.get("dimensions") or {}
            dims_txt = ", ".join(f"{k}={v}" for k, v in dims.items())
            head = f"  - {oid} ({otype})"
            if dims_txt:
                head += f": {dims_txt}"
            bb = d.get("bounding_box") or {}
            if bb.get("min") and bb.get("max"):
                head += f"; bbox min {bb['min']} max {bb['max']}"
            lines.append(head)
            subs = d.get("named_subelements") or []
            faces = [s for s in subs if s.get("kind") == "face"]
            edges = [s for s in subs if s.get("kind") == "edge"]
            if faces:
                ftxt = ", ".join(
                    f"{s['ref']}" + (f"({s['hint']})" if s.get("hint") else "")
                    for s in faces)
                lines.append(f"      faces: {ftxt}")
            if edges:
                etxt = ", ".join(s["ref"] for s in edges)
                lines.append(f"      edges: {etxt}")
        return "\n".join(lines) + "\n\n"

    def _overview_block(self, overview: Optional[dict]) -> str:
        if not overview:
            return "The document is currently empty or its content is unknown.\n\n"
        objs = overview.get("objects", [])
        if not objs:
            return (f"Active document '{overview.get('document_name', '?')}' is empty "
                    f"(no objects yet).\n\n")
        lines = [f"Active document '{overview.get('document_name', '?')}' contains "
                 f"{overview.get('object_count', len(objs))} object(s):"]
        for o in objs:
            lines.append(f"  - id={o.get('id')} type={o.get('type')} "
                         f"label={o.get('label')}")
        return "\n".join(lines) + "\n\n"

    # -- normalization + validation --------------------------------------------

    def _normalize(self, reply: Any) -> Dict[str, Any]:
        """
        Validate the model reply structurally and split valid/invalid actions.

        Besides the historical keys, the plan carries an "ordered" list that
        preserves the position of every usable action (ADR 0018):
          {"status": "valid",   "action": {...}}
          {"status": "fixable", "action": {...}, "missing": [...],
           "bad_enum": {param: [allowed]}, "errors": [...]}
        A FIXABLE action failed validation ONLY for missing required params
        (each with a known default) and/or invalid enum values: the engine may
        resolve it by asking the user a templated question instead of dropping
        it. Engines/tests that ignore "ordered" keep the v0.12 behaviour
        (fixable actions are simply not in valid_actions).
        """
        if not isinstance(reply, dict):
            raise PlanError("the model reply is not a JSON object")

        actions = reply.get("actions", [])
        if not isinstance(actions, list):
            raise PlanError("the 'actions' field is not a list")

        clarification = reply.get("clarification") or None
        notes: List[str] = []
        valid: List[dict] = []
        ordered: List[dict] = []

        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                notes.append(f"action #{i}: ignored (not an object)")
                continue
            atype = action.get("type", "command")
            if atype == "python":
                code = action.get("code")
                if not isinstance(code, str) or not code.strip():
                    notes.append(f"action #{i}: python action without code, dropped")
                    continue
                entry = {"type": "python", "code": code,
                         "reason": action.get("reason", "")}
                valid.append(entry)
                ordered.append({"status": "valid", "action": entry})
            elif atype == "command":
                invocation = {"cmd": action.get("cmd"),
                              "params": action.get("params", {}) or {}}
                errors = fake_brain.validate_invocation(invocation, self.catalog)
                if fake_brain.is_blocking(errors):
                    info = questions.classify(invocation, self.catalog)
                    if info["fixable"]:
                        # Do NOT drop it: the engine can ask the user (ADR 0018).
                        ordered.append({
                            "status": "fixable",
                            "action": {"type": "command", **invocation},
                            "missing": info["missing"],
                            "bad_enum": info["bad_enum"],
                            "errors": list(errors),
                        })
                        continue
                    notes.append(
                        f"action #{i} ({invocation['cmd']}): dropped, "
                        f"{'; '.join(errors)}")
                    continue
                if errors:  # non-blocking warnings
                    notes.append(f"action #{i} ({invocation['cmd']}): "
                                 f"{'; '.join(errors)}")
                entry = {"type": "command", **invocation}
                valid.append(entry)
                ordered.append({"status": "valid", "action": entry})
            else:
                notes.append(f"action #{i}: unknown action type '{atype}', dropped")

        return {
            "actions": actions,
            "valid_actions": valid,
            "ordered": ordered,
            "notes": notes,
            "clarification": clarification,
            "ask": self._normalize_ask(reply.get("ask")),
        }

    @staticmethod
    def _normalize_ask(ask: Any) -> Optional[dict]:
        """
        Normalize the model's optional clarification question (ADR 0019).
        Returns {"question", "options", "default"} or None when absent/unusable.
        The default may be None here; the ENGINE enforces the golden rule
        (no default = treat as a plain clarification, never a blocking wait).
        """
        if not isinstance(ask, dict):
            return None
        question = str(ask.get("question") or "").strip()
        if not question:
            return None
        options = [str(o).strip() for o in (ask.get("options") or [])
                   if str(o).strip()]
        return {"question": question, "options": options[:6],
                "default": ask.get("default")}
