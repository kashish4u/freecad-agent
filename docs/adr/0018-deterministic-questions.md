# ADR 0018 - Deterministic clarification questions (user.question, protocol 0.2.0)

- Status: accepted
- Date: 2026-07-10 (Phase 7, "towards Jarvis" - star 4, level 1)
- Depends on: ADR 0008 (cooperative cancellation), ADR 0017 (agentic feature loop)

## Context

The agent sometimes receives a request with a missing or ambiguous value
("drill a hole in the box" - which diameter?). Until v0.12 the plan validator
silently DROPPED such actions with a log note, and the user had to rephrase.
Phase 7 makes the agent able to ASK - but with two hard constraints decided in
the Phase 7 plan:

1. **Principle 4 is untouched**: the agent never asks *permission* to act
   (reversibility covers the risk, ADR 0006 of the constitution). Questions are
   only for missing information or project-forking ambiguity.
2. **Realism on a 4B local model**: the questions of this ADR are produced
   DETERMINISTICALLY by the engine from the validator's findings - zero
   inference, they work identically with any model. (The model-produced
   `{"ask": ...}` channel is level 2, ADR 0019.)

## Decision

### Golden rule
Every question carries a **default** the user can accept with one click. A
parameter with no sensible default (object references such as `target`, `a`,
`b`, `face`) never produces a question: the action is dropped exactly as in
v0.12. Corollary: questions never become an interrogation - hard cap
`MAX_QUESTIONS_PER_RUN = 3` **per run** (not per feature plan: an 8-feature
agentic run would otherwise allow 24 interruptions). Past the cap the engine
proceeds with the declared default, visible in the log.

### Question generator (engine/questions.py, pure stdlib)
- `DEFAULTS`: a (cmd, param) table of sensible defaults (mm/degrees).
- `classify(invocation, catalog)`: an invalid invocation is **fixable** iff its
  only blocking problems are missing required params (all with defaults) and/or
  invalid enum values. Wrong types / violated minimums stay on the classic
  drop + self-correction path.
- `build_question(cmd, param, catalog, bad_value)`: templated English text with
  the proposed default; for enums the options are the allowed values.
- `coerce_answer(...)`: converts the (often string) answer to the parameter type;
  the normal validator re-judges the patched action downstream (principle 7).

### Brain
`Brain._normalize` now also returns an `ordered` list preserving action
positions, each entry `{"status": "valid"|"fixable", ...}`. Fixable actions are
NOT in `valid_actions` (v0.12 consumers unaffected); engines that ignore
`ordered` (stub brains in tests) behave exactly as before.

### Engine flow (bridge_server.Session)
`_resolve_questions(task_id, plan)` walks `ordered`: valid actions pass
through; for each fixable one it fills the gaps by asking (`_ask_user`), then
re-validates. It runs in BOTH flows (flat and agentic; the cap is shared per
run). The panel checkbox **"Ask me when unsure"** travels with every
`user.prompt` (`ask_when_unsure`); **OFF reproduces v0.12 byte for byte**: no
question, no default, fixable actions dropped with the classic note.

### Protocol 0.2.0 (ADDITIVE - no existing method changed)
- `user.question` (engine -> addon): `{question_id, task_id, cmd, param,
  question, options, default}`. The add-on shows a NON-modal card and returns
  `{ok:true}` immediately - the user may take minutes.
- `user.answer` (addon -> engine): `{question_id, value? | use_default:true}`.
  Unknown/expired ids return ok=false and are harmless.
- The wait is a **cancellable checkpoint** (ADR 0008 style): polled in 0.25 s
  slices; `user.cancel` closes the question and stops the run at once; a
  safety timeout (`QUESTION_WAIT_TIMEOUT = 300 s`) falls back to the default,
  declared in the log.
- **Old add-on (protocol 0.1.0)**: the first `user.question` call fails with
  method-not-found; the engine remembers it for the session and degrades to
  SILENT DEFAULTS + log (same degradation pattern as transaction.begin in
  ADR 0017). The handshake logs, never rejects, a version mismatch.

### UI (panel.py)
- Question CARD above the log: question text, clickable option buttons, a free
  text field, and a "Use default" button. NO modal dialog (a modal blocks all
  of FreeCAD - anti-Jarvis, explicitly rejected in the Phase 7 plan).
- Checkbox "Ask me when unsure", ON by default.
- The old dynamic local/remote privacy indicator became a FIXED **"Local AI"**
  badge (Phase 7 decision: the project is local-only; there is no remote).

## Alternatives considered

- *Modal QMessageBox*: rejected - blocks the whole FreeCAD session and reads
  as a permission prompt.
- *Cap per feature plan*: rejected - multiplies interruptions in agentic runs.
- *Asking also without a default*: rejected - breaks the one-click golden rule
  and turns clarification into interrogation.
- *Bumping the protocol to 1.0 / rejecting old peers*: rejected - v0.12 users
  must keep working with no migration (Phase 7 compatibility rule).

## Consequences

- Versions: engine+addon 0.12.2-phase7, protocol 0.2.0, commands schema
  UNCHANGED (v0.5.0).
- New test `tests/test_questions.py` (7 scenarios: templates, answer, default,
  cancel, checkbox OFF, old add-on, cap); suite 23/23.
- Known caveat: while a question waits, the run (and, in the agentic loop, the
  feature's undo group) stays open - the user should answer or cancel rather
  than edit the document, same caveat as ADR 0017 repairs.
