# ACTION PLAN — Robust FreeCAD-Engine

**Status:** in progress
**Goal:** an engine that reliably draws *any* part and *any* assembly from a small
model, and never silently produces nothing.

---

## 1. Root problem (the spoon failure, diagnosed 2026-10-06)

The user typed **"create spoon"** and the result was wrong. Two-channel failure,
in order:

1. **`FreeCADPart` import crash** — the free-Python channel (`addon/ai_copilot/executor/python_exec.py`)
   did not alias `FreeCADPart -> Part`. The model's natural `import FreeCADPart`
   threw `ModuleNotFoundError: No module named 'FreeCADPart'`.
   **FIXED** (deployed). The model has since switched to `import Part`, and that
   import now works.

2. **Wrong-API guesses** — the model still does not know this FreeCAD's `Part`
   module. It calls `Part.GeometryFusion`, `Part.union`, `Part.makeGeometryFusion`,
   none of which exist here. The repair loop feeds each error back to the model,
   but the model only *varies* the wrong guess (`GeometryFusion` → `union` →
   `makeGeometryFusion`) instead of succeeding. The spoon fails again.

**This is the real bug.** The free-Python channel trusts the model to guess the
exact FreeCAD API. The model is small and wrong. We must stop trusting it.

---

## 2. This turn's fix — geometry compatibility shim (Phase 1 foundation)

**Principle (dumb-model / smart-kernel):** the kernel accepts the model's
imprecise API names and resolves them onto correct `Shape` methods, so a wrong
guess still produces the right geometry.

**Change:** in `addon/ai_copilot/executor/python_exec.py`, after
`env["Part"] = Part` (and the `FreeCADPart` alias), install a one-time shim that
maps the model's common guesses to the correct call:

| Model guesses | Resolves to | Correct FreeCAD call |
|---|---|---|
| `Part.makeGeometryFusion([...])`, `Part.GeometryFusion`, `Part.union(a,b)` | fuse chain | `s.fuse(...)` |
| `Part.cut([...])`, `Part.difference` | subtract chain | `s.cut(...)` |
| `Part.intersect([...])`, `Part.common`, `Part.intersection` | overlap chain | `s.common(...)` |
| `Part.makeFusion(a,b,t)` | fuse | `s.fuse(...)` |

Only installs names that are *missing* (`hasattr` guard) so real FreeCAD
functionality is never shadowed. Guarded by `isinstance(Part, types.ModuleType)`.

**Why structural, not a prompt tweak:** the model will keep guessing wrong
forever; the fix makes the guess harmless regardless of what the model says.

---

## 3. Steps (this turn)

1. ✅ Write this ACTION_PLAN.md.
2. Add the shim to `python_exec.py` (add `import types`; install shim right after
   the `Part`/`FreeCADPart` alias block).
3. Write `tests/test_free_python_compat.py` — headless (mock FreeCAD), asserts the
   shim exists and that `Part.union`/`makeGeometryFusion`/`intersect` produce the
   correct fused shape, and that a real `Part.makeBox` still works.
4. Run `RUN_ALL_TESTS.bat` — expect ALL 27 tests green, no regression.
5. Deploy: run `INSTALL_ADDON.bat` (copies working tree →
   `AppData\Roaming\FreeCAD\v1-1\Mod\FreeCADAgent`). **Restart FreeCAD** for the
   add-on to load the new `python_exec.py`.
6. Commit + push to `origin/main`.

## 4. Honest constraint

A mis-plan must **fail loudly and recover**, not silently produce nothing. The
shim converts the *most common* wrong guesses into correct geometry so the model
stops tripping on API names; genuine geometry errors (thin fillets, invalid
edges) still surface to the model for repair.

## 5. Remaining phases (for later)

- **Phase 2 — profile primitives:** harden closed-profile validation; add T/angle/L
  profiles, tube, lozenge; auto closed-check on sketches.
- **Phase 3 — placement & assembly:** placement-by-reference, body hierarchy,
  boolean/joint assembly, MBD.
- **Phase 4 — perception for assembly.**
- **Phase 5 — deterministic planner + repair.**
- **Phase 6 — headless test harness (integration).**

---

*Next action: implement the shim (step 2).*
