# TEST GUIDE - Phase 7 cumulative test (Sessions 17-22, v0.12.5-phase7)

This is THE single test document for everything built in Phase 7. It covers,
in order: installation, a v0.12 regression pass, the agentic multi-feature
loop (Session 17 - never tested in real FreeCAD yet), clarification questions,
session memory, all the new commands, three whole pilot components, and
cancel/limits. Do the sections IN ORDER: each one builds on the previous.

Every test says exactly WHAT TO TYPE and WHAT YOU SHOULD SEE. If something
differs, note the test number and copy the panel log (and, if asked, the
engine log via "Show engine log") - do not stop the whole session for one
failure unless the section says so.

---

## 0. One-time setup (do this FIRST)

1. Wait for the kDrive sync icon to be GREEN (all files synced).
2. Double-click `INSTALL_ADDON.bat`. It must print **addon 0.12.5-phase7,
   engine 0.12.5-phase7, protocol 0.2.0, commands schema 0.7.0**. If it
   prints older numbers, the sync was not finished: wait and run it again.
3. Start FreeCAD (a full restart if it was open).
4. Optional but useful: run `RUN_ALL_TESTS.bat` once - it must say
   ALL TESTS PASSED (26 tests, no FreeCAD needed).
5. Open the FreeCAD Agent panel and click **Connect**. The engine starts by
   itself; the status turns green. New things you should notice in the panel:
   - a fixed green **"Local AI"** badge (top right) - it never changes;
   - a checkbox **"Ask me when unsure"**, ON by default.
6. Ollama must be running with the usual model pulled (the engine starts it
   for you if it is installed but off).

Create a NEW empty document before every numbered test unless the test says
otherwise (menu File > New).

---

## 1. Regression - the v0.12 behaviour must be intact

These are known-good v0.12 phrases. They must work exactly as before.

- **R1** - `create a box 30x20x15` -> a box appears. Ctrl+Z removes it.
- **R2** - `create a box 40x40x10 and drill a 8 mm hole in the centre` ->
  box with a through hole in the middle.
- **R3** - `round all the edges of Box with radius 2` (after R1 in the same
  document, target name as shown in the model tree) -> rounded box.
- **R4** - `arrange 6 copies of Cylinder in a circle around Z` (first:
  `create a cylinder radius 5 height 10`, then move it 30 mm along X) ->
  a ring of 6 cylinders ORBITING the origin.
- **R5** - structured command (expert mode): pick `create_box`, fill
  length/width/height, Run -> box created. This must work even with Ollama
  off.

PASS when: all five behave as in v0.12. This section protects against
regressions from everything below - if R1-R5 fail, STOP and report.

## 2. Agentic multi-feature loop (Session 17 - FIRST real test ever)

- **F1** - New document. Type:
  `make a 100x60x10 plate with 4 corner holes of 6 mm and rounded vertical edges`
  You should SEE in the log: `decomposing...`, then `decomposed: 3 feature(s)`
  (or similar split), then for each feature: `feature i/N`, `planning`,
  actions, `feature i/N completed`. At the end: a plate with 4 holes and
  rounded vertical edges.
- **F2** - Ctrl+Z once -> the LAST FEATURE disappears as a whole (e.g. the
  rounds), NOT just one action. Ctrl+Z again -> the holes go. This is the
  per-feature undo group.
- **F3** - single-op regression: `create a box 20x20x20` must NOT show any
  feature lines (flat flow, v0.12 identical).

Acceptable on a 4B model: the decomposition may differ (2-4 features) or not
trigger at all on F1 (then the flat flow runs it in one plan - note it, it is
a degradation, not a bug). NOT acceptable: errors, half-applied features.

## 3. Clarification questions (ADR 0018/0019)

- **Q1 (question -> answer)** - New document with a box (R1). Type:
  `drill a hole in the box` (NO diameter). A blue CARD should appear above
  the log: "drill_hole needs a value for 'diameter'. I propose 6 mm." with
  clickable option(s), a free text field and **Use default**. Click the
  option or type `8` and Answer -> the hole is drilled with that value.
- **Q2 (use default)** - same phrase again -> card appears -> click
  **Use default** -> 6 mm hole.
- **Q3 (question -> cancel)** - same phrase -> when the card appears, press
  **Cancel** -> the run stops, NOTHING is drilled, the card disappears.
- **Q4 (checkbox OFF = v0.12)** - untick "Ask me when unsure", same phrase ->
  NO card; the log shows the action was dropped ("missing required
  parameter"), like v0.12. Re-tick the box afterwards.
- **Q5 (model-side ask, MAY OR MAY NOT HAPPEN)** - type something genuinely
  ambiguous, e.g. `drill a mounting hole in the plate` (a plate from F1).
  EITHER a sensible question appears (the model used the "ask" channel), OR
  the engine asks the templated diameter question, OR a declared default is
  used. ALL THREE are PASS on a 4B model. FAIL is only: a permission-style
  question ("may I proceed?") or a hang.
- **Q6 (cap)** - `create a box, a cylinder and drill a hole` phrased with no
  dimensions at all, e.g.: `create a box, then a cylinder, then drill a hole
  in the box` -> at most 3 questions total; any further gaps use declared
  defaults visible in the log ("question cap reached").

## 4. Session memory / anaphora (ADR 0019)

Do these in ONE session (do not restart the engine between them).

- **M1** - `create a cylinder radius 20 height 50` -> cylinder appears.
- **M2** - as a SECOND separate request: `drill a 6 mm hole in its centre` ->
  the hole goes into THAT cylinder (the log may show the memory being used).
  Acceptable on a 4B: if it asks which object - answer it. FAIL: it invents a
  new object or errors out.
- **M3** - restart the engine (Stop engine, then Connect). Repeat M2 alone ->
  now the engine has NO memory of the cylinder; asking for clarification or
  failing gracefully is CORRECT (the memory is session-only, by design).

## 5. New commands - vocabulary A (Session 20)

New document for each.

- **V1** - `create a cone with base radius 10 and height 25` -> cone.
- **V2** - `create a truncated cone, base radius 10, top radius 4, height 20`
  -> frustum.
- **V3** - `create a sphere of radius 12` -> sphere.
- **V4** - `create a torus, ring radius 20, tube radius 3` -> ring/donut.
- **V5** - `draw a 20x10 rectangle and revolve it around the X axis` ->
  a full solid of revolution; the sketch is hidden afterwards.
- **V6** - `draw a 15x8 rectangle and revolve it 180 degrees around the Y axis`
  -> half a revolution.

## 6. New commands - vocabulary B (Session 21)

- **W1 (polygon)** - `make a hexagonal prism, radius 15, 8 mm tall` ->
  six-sided prism (sketch + extrude).
- **W2 (slot)** - `draw a slot 30 mm long and 10 mm wide and extrude it 5 mm`
  -> a rounded slot bar.
- **W3 (polyline)** - `draw a closed polyline through [0,0], [40,0], [40,20],
  [0,20] and extrude it 6 mm` -> a rectangular plate (from the polyline).
- **W4 (loft)** - `loft a 40x40 square into a circle of radius 10, 30 mm
  above it` -> a square-to-round transition solid. Watch the log: the loft's
  profile names are LINKED to the two real sketches automatically.
- **W5 (sweep)** - `draw a circle of radius 4 on the XZ plane, then a
  polyline path through [0,0], [50,0], [50,40], and sweep the circle along
  the path` -> a bent pipe-like solid. (A 4B model may need this phrased in
  two or three separate requests - that is acceptable; note what worked.)
- **W6 (shell)** - `create a box 40x30x20, then hollow it out with 2 mm
  walls, open on top` -> an open box with 2 mm walls.
  **WATCH-POINT (ADR 0021): check the walls grow INWARD** (outer size stays
  40x30x20). If the box instead GREW outward, note it: it is a one-line fix
  (the sign of the thickness value) at the fix step of this session.

## 7. Pilot components - one paragraph each (Session 22, the "Jarvis" test)

New document for each. Type the WHOLE paragraph as ONE request. Expect the
per-feature loop: decomposition, features one at a time, maybe a question.
For each pilot: note how many features, how many questions, what failed and
what recovered. Two out of three completing end-to-end = the phase goal.

- **P1 (drilled flange)** - `Make a circular flange: a disc of radius 40 and
  thickness 8, with a centred hole of diameter 20, and 4 bolt holes of
  diameter 6 arranged in a circle of radius 30 around the centre, and round
  the top edges with radius 1.`
- **P2 (bracket with pocket)** - `Make a mounting bracket: a base plate
  60x40x10, cut a rectangular pocket 30x20 and 5 mm deep into its top face,
  drill two 5 mm holes at positions [10, 20] and [50, 20], and chamfer the
  vertical edges by 1.`
- **P3 (stand with pattern)** - `Make a support stand: a base plate 80x80x6,
  a central boss cylinder of radius 15 and height 30 on top of it, drill a
  10 mm hole through the boss, and arrange 8 holes of 4 mm on a circle of
  radius 32 around the centre of the plate.`

Known 4B limits (NOT bugs, from v0.12 testing): phrases that require the
model to CALCULATE coordinates (trigonometry) fail - that is why P1/P3 use
the polar `array`/explicit positions. If a pilot stops at a failed feature,
the completed features remain (Ctrl+Z undoes one feature at a time) - that
behaviour itself is a PASS for the recovery machinery.

## 8. Cancel and limits

- **C1** - start P1 again and press **Cancel** while it says `thinking` ->
  the run stops at the next checkpoint; NOTHING new is applied after the
  click; already-completed features remain undoable.
- **C2** - start a request that opens a question card and press Cancel -> the
  card closes and nothing runs (same as Q3, now mid-plan).
- **C3** - `create a box` with Ollama OFF (quit it from the tray) -> polite
  refusal + structured commands still work; turn Ollama back on -> natural
  language resumes by itself.

## 9. What to record

For the fix step and the release notes, keep a simple list:
| Test | OK/FAIL/NOTE | What happened |
|---|---|---|

The failures get fixed in this same session (Session "collaudo"); then the
version is bumped to 0.13.0 and the release flow starts. Nothing is public
before that point.
