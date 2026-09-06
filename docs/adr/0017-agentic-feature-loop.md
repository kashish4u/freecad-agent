# ADR 0017 — Agentic per-feature loop (plan → execute → perceive → replan)

- **Status:** accepted (Session 17, 2026-07-10; approved by Marco)
- **Phase:** 7 ("Towards Jarvis"), step 1 of the approved Phase-7 plan
- **Versions:** engine + add-on `0.12.1-phase7`; protocol `0.1.0` (additive
  extension only, see §4); command schema unchanged (`0.5.0`, still 14 commands)

## Context

Up to v0.12.0 the engine handles a natural-language request as ONE flat plan:
perceive once, ask the model for a single list of actions, execute them with
bounded per-action self-correction (ADR 0005) and id-chaining (ADR 0010 / 0013 /
0016). This works for requests up to a handful of actions, but a WHOLE component
("a 100x60x10 plate with 4 corner holes and rounded vertical edges") needs a
plan longer than a small local model (4B, ctx 8192) can produce in one shot
without drift, and offers no intermediate recovery: if action 7 of 12 goes
wrong, the remaining actions run against geometry that no longer matches the
plan's assumptions.

The Phase-7 brainstorm (Session 16) evaluated three options for "whole model
from one prompt":

- **A — flat plan, strengthened prompts:** rejected; does not scale, no
  intermediate perception, one bad action poisons the rest.
- **B — two stages (decompose, then plan everything up front):** rejected; it
  plans blind. Later features depend on the REAL geometry produced by earlier
  ones (auto-generated ids, face positions), which is only knowable by
  perceiving after execution.
- **C — agentic loop per feature:** chosen. Decompose the request into
  features, then for each feature: plan → execute → perceive → replan the next
  with the true document state. It is the natural extension of perception
  (ADR 0005) plus id-chaining (0010/0013/0016).

The project is local-only by decision (Session 16): everything here must work
acceptably on a 4B model. There is no "use a bigger model" escape hatch.

## Decision

### 1. Feature decomposition: a separate, tiny inference call

Before planning, the engine asks the model to split the request into an ordered
list of features via a NEW brain method `Brain.decompose(request, overview)`.
The decomposition prompt is deliberately minimal — no command catalog, no long
few-shot examples — so the extra inference is as cheap as possible. The model
answers `{"features": [...]}`; an empty list means "single-step request".

- `decompose()` NEVER raises: any transport/parse problem returns `[]`.
- **0 or 1 feature ⇒ the classic v0.12 flat flow runs, unchanged.** This is the
  retro-compatibility guarantee ("1 feature = current behaviour") and the
  graceful-degradation path (principle 9): a model that cannot decompose simply
  makes the agent behave exactly like v0.12.
- A scripted/stub brain without a `decompose` attribute is treated as "no
  decomposition" (`hasattr` guard), so every existing headless test and any
  third-party brain keeps working untouched.

*Alternative rejected:* one dual-mode prompt where the model itself chooses
between `{"actions"}` and `{"features"}`. No extra inference, but it puts a
fragile mode decision inside the well-tested planning prompt and risks
regressions on the simple phrases that already work on a 4B.

### 2. Run-state in the engine `Session`

A `_RunState` object carries, across the whole run: the id-chaining state that
used to be local to one plan (`last_profile_id`, `last_created_id`,
`created_count`, `known_ids`, `replaced`), the accumulated `results`, and
`done_features` — one summary line per completed feature ("feature 1: create
the base plate -> created Box"). Only this SUMMARY is fed back to the model
(realism: ctx 8192 — never the full transcript). The action-execution loop was
extracted to `Session._execute_actions(...)`, shared verbatim by the flat path
and the per-feature path, so chaining behaves identically in both.

### 3. The loop, its bounds and cancellation

For each feature `i` of `N` (after `agent.status` phase `feature`,
message `feature i/N: <text>`):

1. **perceive** — fresh `perception.overview` + geometric RAG details
   (ADR 0005): earlier features changed the document.
2. **plan** — `Brain.plan(request, overview, details, feature=..., done=...,
   feedback=...)`: same catalog/system prompt as always, plus a run-state block
   ("Overall request … Features already built … Now plan ONLY this feature").
3. **execute** — the feature's actions inside ONE undo group (§4), with the
   existing per-action bounded repair (`MAX_REPAIR_ATTEMPTS = 2`, ADR 0005).
4. **replan** — if any action of the feature ultimately failed, the group is
   aborted (the feature leaves NO trace), the run-state is restored to its
   pre-feature snapshot, and the feature is re-planned once with the failure
   text as `feedback`. Bound: `MAX_REPLANS_PER_FEATURE = 1`.

Bounds (principle 8 — no infinite loops): `MAX_FEATURES = 8` per run (extra
features are dropped with a visible note; the plan documents that >6-8 features
is beyond a 4B anyway), `MAX_REPLANS_PER_FEATURE = 1`, plus the pre-existing
per-action repair bound. If a feature still fails after its replan, the run
STOPS with a clear error; completed features remain (each undoable one by one).
Continuing past a failed feature was rejected: later features usually depend on
the failed one, and error cascades on a slow local model waste minutes.

**Cancellation (ADR 0008) is honoured at every checkpoint:** after perception,
after decomposition, after each plan/replan inference, and before every single
action (inherited from `_execute_actions`). A cancel that lands mid-feature
aborts that feature's undo group, so no half-feature is left behind.

### 4. One FreeCAD transaction per feature (Ctrl+Z per feature)

Two NEW bridge methods (engine → add-on), an additive protocol extension —
protocol version stays `0.1.0`, no existing method changes shape:

- `transaction.begin {label}` → add-on calls
  `FreeCAD.setActiveTransaction(label)`: from now on, the per-action
  transactions the executor already opens (`transaction.undoable`, principle 6)
  are grouped by FreeCAD under ONE undo entry.
- `transaction.end {abort}` → add-on calls
  `FreeCAD.closeActiveTransaction(abort)`: commit the group as a single undo
  step, or roll the whole feature back.

Consequences: **Ctrl+Z undoes one FEATURE at a time** in multi-feature runs,
and a failed/cancelled feature rolls back atomically. The flat (0-1 feature)
path does NOT use groups, so v0.12 phrases keep their per-action undo, byte for
byte.

Degradation (principle 9): if the add-on does not know `transaction.begin`
(older add-on) or FreeCAD lacks `setActiveTransaction`, the engine logs it
once, falls back to per-action transactions, and — because a failed feature can
then not be rolled back atomically — SKIPS replans (re-running a partially
applied feature could double-apply actions). Everything else still works.

*Alternatives rejected:* piggybacking a `tx_group` field on `command.execute`
(needs an out-of-band "close" signal anyway — uglier than two explicit
methods); holding one `doc.openTransaction` open across actions (FreeCAD
auto-commits on nested opens; and an open doc-transaction across a slow repair
inference would swallow the user's own edits); a single `command.execute` per
feature (would disable per-action repair).

### 5. What does NOT change

No new vocabulary command (the 14 v0.12 commands are untouched, schema 0.5.0).
No panel change: the existing `agent.status` stream simply carries the new
`feature i/N` messages. `command.request` (expert mode) is untouched. The known
limitation stands: phrases requiring the model to COMPUTE coordinates
(trigonometry) remain beyond a 4B; guides use explicit positions.

## Consequences

- One extra (small) inference per prompt for decomposition — a few seconds even
  for simple phrases. Accepted for correctness; if it proves annoying in real
  use, a panel toggle can be added when the panel is next touched (Session 18).
- The engine's `on_user_prompt` is now a thin orchestrator; the execution loop
  lives in `_execute_actions` and is shared by both paths.
- During a feature's actions (including repair inferences) an undo group is
  open on the document; the user is expected to watch the run, not edit
  concurrently. Documented in the test guide.
- Foundation laid for Session 18/19: the run-state block is where session
  memory will plug in; the per-feature checkpoints are where `user.question`
  waits will live.
