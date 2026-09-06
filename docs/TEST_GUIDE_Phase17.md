# Test guide — Phase 17: the agentic per-feature loop (ADR 0017)

This guide validates Session 17: the engine can now split a request into
FEATURES and build them one at a time (plan → execute → look at the result →
plan the next). Each feature is ONE undo step: **Ctrl+Z removes a whole
feature**, not a single action. Simple requests behave exactly like v0.12.

Expected versions after updating: **engine 0.12.1-phase7, add-on
0.12.1-phase7** (the installer prints them). Protocol 0.1.0, same 14 commands.

---

## 0. Update first (as always)

1. Wait until kDrive shows the green "synced" icon.
2. Double-click `INSTALL_ADDON.bat`. Check it prints **0.12.1-phase7**.
3. Restart FreeCAD completely.
4. Open the FreeCAD Agent panel → **Connect**. The engine starts by itself;
   the status line should say the engine is running.

> During an agentic run, just watch: don't edit the model yourself while the
> agent is working (each feature keeps an undo group open until it finishes).

---

## 1. REGRESSION block — v0.12 phrases must behave exactly as before

Start from a NEW empty document (File → New) for each phrase. Type each phrase
in the natural-language box and press Send.

| # | Phrase | Expected result |
|---|--------|-----------------|
| R1 | `create a box 40x40x10 and drill a 8 mm hole in the centre` | Box with a centred through hole, same as v0.12 |
| R2 | `create a box 30x30x20` then `round the top edges of Box with radius 3` | Top edges filleted, same as v0.12 |
| R3 | `create a cylinder radius 8 height 25` then `arrange 6 copies of Cylinder in a circle of radius 40 around Z` | 6 cylinders ORBITING the file origin in a ring, same as v0.12 |

What is NEW and fine to see: a brief status line
`checking whether the request splits into features` before each run. It adds a
few seconds (one small extra question to the model). Everything else — result,
log style, Ctrl+Z per action — must be IDENTICAL to v0.12.

If a v0.12 phrase accidentally splits into features (you would see
`feature 1/2 ...` in the log): note the phrase and tell the developer — the
result should still be correct, but we want to know.

**PASS criteria:** same geometry as v0.12, no errors, no behaviour change.

---

## 2. NEW block — one phrase, several features

Start from a NEW empty document. Type ONE phrase:

```
make a 100x60x10 plate with 4 corner holes of 6 mm and rounded vertical edges
```

Watch the log. You should see the new cycle, one feature at a time:

1. `N feature(s): 1. create the base plate ... 2. drill 4 holes ... 3. round ...`
2. `feature 1/3: ...` → planning → actions → `feature 1/3 completed`
3. `feature 2/3: ...` → (the engine LOOKS at the document again first) → ...
4. `feature 3/3: ...` → ... → `3/3 feature(s) done (... Ctrl+Z undoes one
   feature at a time)`

Expected model: a 100×60×10 plate, a 6 mm hole near each corner, vertical
edges rounded.

**Now the key test — Ctrl+Z per feature:**

- Press **Ctrl+Z once** → ALL the fillets disappear together (last feature).
- Press **Ctrl+Z again** → ALL 4 holes disappear together.
- Press **Ctrl+Z again** → the plate disappears.
- (Edit → Undo also shows the entries named `FreeCAD Agent: feature i/N: ...`.)

Optional second phrase (new document):

```
make a 80x80x15 base with a round pocket of radius 10, 5 mm deep, in the top
face and chamfer the top edges by 2
```

Expected: base + centred round pocket + chamfered top edges, built as
separate features, each undoable in one Ctrl+Z.

**Honest limits (not bugs) with a small 4B model:**

- The decomposition may be imperfect (e.g. 2 features instead of 3, or holes
  not exactly at the corners). If the holes land in wrong places, retry with
  explicit positions, e.g.:
  `make a 100x60x10 plate with 6 mm holes at [10,10], [90,10], [90,50], [10,50]
  and rounded vertical edges`
  (computing coordinates is the known 4B limit, see Phase 13 guide).
- If a feature fails even after its automatic retry, the run STOPS with a
  clear message; the features already built remain (each undoable).
- Runs are bounded: max 8 features, 1 replan per feature — the agent can never
  loop forever.

**PASS criteria:** the log shows the per-feature cycle; the final model is
correct (or fails honestly with a clear message); Ctrl+Z removes one FEATURE
at a time.

---

## 3. Cancel during a run (30 seconds)

Send the multi-feature phrase from block 2 again (new document) and press
**Cancel** while the log says `planning feature 2/3 ...`.

Expected: the run stops at the next safe point; **nothing of feature 2 is
built**; feature 1 (the plate) remains and one Ctrl+Z removes it. No errors.

---

## 4. Troubleshooting

- Log says `per-feature undo not available ... falling back to per-action
  undo`: the add-on was not updated — rerun `INSTALL_ADDON.bat` (check it
  prints 0.12.1-phase7) and restart FreeCAD.
- Everything runs as one flat plan (no `feature i/N` lines) even for the block-2
  phrase: the model chose not to decompose. Not a bug (graceful fallback), but
  report it: we may need to tune the decomposition prompt.
- Engine does not start / no answer: same checks as Phase 12 guide (Show
  engine log button, `~/.freecad-agent/engine.log`).
