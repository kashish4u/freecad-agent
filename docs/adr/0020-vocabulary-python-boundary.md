# ADR 0020 - The vocabulary/Python boundary (and vocabulary A: cone, sphere, torus, revolve)

- Status: accepted
- Date: 2026-07-10 (Phase 7, "towards Jarvis" - star 2)
- Depends on: ADR 0004 (free Python channel), ADR 0006/0010/0011/0013 (executor-side
  resolution of fragile references), ADR 0017 (agentic loop)

## Context

The structured vocabulary (principle 5) grows phase by phase, and every new
command costs prompt budget on a ctx-8192 local model, an implementation, tests
and maintenance. Meanwhile the agent has had a FREE PYTHON channel since Phase 2
(ADR 0004): transparent (the exact code is shown in the panel's red banner
before it runs) and safe (it executes inside an undoable transaction). Phase 7
needed a written rule for WHAT deserves a command and what does not - otherwise
the vocabulary grows without a criterion until it saturates the model.

## Decision

### Admission criteria for the vocabulary (ALL must hold)
1. **Common**: the operation appears in everyday part modelling (not a niche).
2. **Robustly parameterizable by a small model**: its parameters are numbers,
   short enums or object ids - NEVER indices of sub-elements or free 3D
   vectors, which a 4B model gets wrong (lessons of ADR 0006/0010/0011: the
   executor resolves fragile references, the model never guesses them).
3. **No new dependencies**: implementable with core FreeCAD (Part workbench,
   Sketcher) - principle 2, no Draft, no external libraries (ADR 0004).

### Free Python is the OFFICIAL answer to the long tail
Anything outside the criteria (threads, helices, surfacing, one-off tricks) is
SUPPOSED to go through the free Python channel - it is a documented feature,
not a fallback of shame: the model is explicitly told "if no structured command
fits, propose Python", the user sees the exact code (red banner, principle 5)
and Ctrl+Z reverts it (principle 6). The README documents this as the escape
hatch for the long tail.

### Vocabulary A added by this ADR (schema v0.6.0, 14 -> 18 commands)
| Command | FreeCAD object | Notes |
|---|---|---|
| `create_cone` | `Part::Cone` | radius1 base, radius2 top (0 = pointed; equal radii rejected: that is a cylinder) |
| `create_sphere` | `Part::Sphere` | radius, optional placement |
| `create_torus` | `Part::Torus` | radius1 ring, radius2 tube; tube >= ring rejected (self-intersection) with a clear message |
| `revolve` | `Part::Revolution` | target profile, angle (default 360), axis as LETTER 'X'/'Y'/'Z' (ADR 0011), base point (default origin); consumes and hides its sketch like extrude |

Naming: `create_*` for primitives, consistent with `create_box`/`create_cylinder`
(the model sees one convention). `revolve` joins `extrude` in the engine's
profile-chaining (`PROFILE_CONSUMERS`, ADR 0010) and consumed-target chaining
(`CONSUMING_PARAMS`, ADR 0016), so "draw a profile and revolve it" needs no
predicted sketch names.

### Rejected / deferred
- PartDesign Body/Pad/Revolution: still too state-sensitive (already rejected
  in ADR 0014); future ADR if ever.
- Helix/thread primitives: long tail -> free Python.
- Surface operations: out of scope for Phase 7 (plan decision).

## Consequences

- Schema v0.6.0; engine+addon 0.12.4-phase7; protocol untouched (0.2.0).
- Brain prompt: ONE compressed rule line (revolve) + two compact few-shot
  examples (cone; sketch->revolve) - prompt budget guarded.
- questions.py defaults extended (cone/sphere/torus) so ADR 0018 can ask about
  their missing parameters.
- Mock: toy shapes for Cone/Sphere/Torus/Revolution; new
  `tests/test_primitives.py` (contract alignment schema<->REGISTRY included);
  suite 25/25.
