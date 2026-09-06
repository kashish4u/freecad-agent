# ADR 0019 - Model-side questions ("ask") and compressed session memory

- Status: accepted
- Date: 2026-07-10 (Phase 7, "towards Jarvis" - stars 4 level 2 + 5)
- Depends on: ADR 0017 (agentic loop), ADR 0018 (deterministic questions)

## Context

ADR 0018 gave the engine a deterministic way to ask about missing parameters.
Two pieces of the "Jarvis" experience were still missing:

1. **Level 2 of the interlocution**: sometimes only the MODEL can tell that a
   request forks ("a mounting hole" - the diameter matters and is not in the
   text). It needs a channel to ask - realistically: a 4B model may never use
   it, and level 1 (ADR 0018) must keep working alone (principle 9).
2. **Session memory**: every request used to be a world of its own. "Now drill
   it in the centre" must resolve against the cylinder created two requests
   ago, within a ctx-8192 budget.

## Decision

### Model ask (level 2)
- Reply format: the model may add `"ask": {"question", "options", "default"}`
  to its JSON (system-prompt rule + one few-shot example, deliberately
  compressed). `Brain._normalize` surfaces it as `plan["ask"]` (or None).
- The engine (`Session._settle_model_ask`) forwards it through the SAME
  `user.question` channel as ADR 0018 (same card, same cancellable wait, same
  per-run cap `MAX_QUESTIONS_PER_RUN = 3` shared with level 1), then folds the
  answer into the request text as `"(Clarified: Q -> A)"` and REPLANS.
  - Folding into the text (instead of a new prompt field) needs no new
    plumbing: the clarification automatically reaches later features of an
    agentic run and the repair prompts, and any brain keeps working.
  - Bounded loop: a model that always asks is stopped by the cap - one replan
    per settled ask, never an infinite ask/replan cycle.
- Golden rule enforced: an ask WITHOUT a default never blocks - it degrades to
  a plain clarification message (the v0.12-style outcome). "Ask me when
  unsure" OFF ignores the ask the same way. Permission questions remain
  forbidden (principle 4): the prompt says so explicitly.

### Session memory
- The engine keeps `Session._history`: ONE line per finished request -
  `"<request>" -> <outcome> [created: ids]` - sliding window
  `MEMORY_MAX_LINES = 8`, each line capped at `MEMORY_LINE_MAX = 160` chars.
  Summaries, never transcripts (ctx-8192 realism). RAM only: the memory dies
  with the engine process (privacy; no persistence by design).
- Recording is a wrapper around `user.prompt` (`on_user_prompt` delegates to
  `_handle_user_prompt`, then `_remember_run`), so every outcome (done,
  cancelled, failed, needs-info) is remembered uniformly.
- The window is passed to `Brain.plan(history=...)` and `decompose(...,
  history=...)`; the brain renders an "EARLIER IN THIS SESSION" block (hard
  char budget, ~250 tokens) plus one anaphora hint line ("pronouns like 'it'
  refer to the objects above"). The decompose prompt gets only the last 3
  lines (it must stay tiny).
- **Compatibility**: the `history` kwarg is passed only if the brain's
  signature accepts it (`inspect.signature`) - stub brains and old tests are
  untouched (adapt, don't exclude).

## Alternatives considered

- *A dedicated `answers` prompt field*: rejected - more plumbing, breaks stub
  brains, and the folded text already reaches replans/features/repairs.
- *Persisting memory to disk*: rejected - privacy and simplicity; the Phase 7
  plan explicitly keeps the memory session-scoped.
- *Full conversation transcripts*: rejected - blows the 8192 context on the
  reference 4B model; summaries are the honest budget.
- *Separate cap for model asks*: rejected - one budget per run keeps the
  "never an interrogation" promise regardless of who asks.

## Consequences

- Versions: engine+addon 0.12.3-phase7; protocol UNCHANGED (0.2.0 - the model
  ask rides the existing user.question); commands schema UNCHANGED (v0.5.0).
- New test `tests/test_session_memory.py` (6 scenarios: ask/answer/replan, ask
  without default, ask loop bound, memory in the prompt with simulated
  anaphora, sliding window, checkbox OFF); suite 24/24.
- Honest limit (documented for the guide): whether the 4B model actually emits
  `ask` is a bet; if it never does, level 1 (ADR 0018) provides the questions.
  Anaphora quality depends on the model reading the memory block - the guide
  treats "sensible question or declared default" both as PASS.
