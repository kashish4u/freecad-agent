# ADR 0021 - Vocabulary B: loft/sweep/shell, rich sketch profiles, multi-profile chaining

- Status: accepted
- Date: 2026-07-11 (Phase 7, "towards Jarvis" - star 2, second half)
- Depends on: ADR 0006/0010/0011/0013/0016 (executor-side reference resolution),
  ADR 0020 (vocabulary admission criteria)

## Context

The remaining Part-workbench operations of the Phase 7 plan take MULTIPLE
profiles (loft: an ordered list; sweep: a profile plus a path) or a FACE choice
(shell: which face stays open). Neither fits the existing chaining machinery:
ADR 0010 tracks ONE pending sketch, and face indices must never be guessed by
the model (principle 7). The sketch profiles themselves were limited to
rectangle/circle.

## Decision

### Rich sketch profiles (create_sketch / sketch_on_face, schema v0.7.0)
- `polygon`: regular, `sides` >= 3 inscribed in circumradius `radius`.
- `slot` (asola): overall `length` x `width`, two lines + two semicircular
  ARCS; `length <= width` rejected ("that is a circle").
- `polyline`: CLOSED wire through `points` [[x,y],...]; an optional third
  number [x, y, sagitta] turns the edge to the NEXT point into an ARC bulging
  by that height (positive = left of travel). The chord+sagitta circle maths
  lives in the EXECUTOR (`_arc_from_sagitta`), never in the model.
- Shared `draw_profile` dispatch; rectangle/circle code paths byte-identical
  to v0.12 (they are validated in real FreeCAD).

### New commands (schema v0.7.0, 17 -> 20 entries)
| Command | FreeCAD object | Notes |
|---|---|---|
| `loft` | `Part::Loft` | `profiles` = ordered LIST of sketch ids; solid default true; consumed profiles hidden |
| `sweep` | `Part::Sweep` | `profile` + `path` (whole object as Spine - no sub-edge picking by the model); Frenet default true |
| `shell` | `Part::Thickness` | `target` + `thickness`; the OPEN face resolved from `where` top/bottom by the shared `select_face_ref` helper; value applied NEGATIVE (walls grow inward, outer dimensions preserved) |

`sketch_on_face` keeps its historical local face resolver (validated in real
FreeCAD); `select_face_ref` in `_common.py` serves the new commands.

### Multi-profile chaining (engine)
The run-state (ADR 0017) gains `pending_profiles`: every sketch created in the
run and not yet consumed, in creation order (covered by snapshot/restore for
per-feature rollbacks). Before execution, `_rewrite_profile_lists` (pure)
replaces the UNKNOWN references of a loft/sweep - loft's list entries, sweep's
profile then path - with that pool, oldest first, each id used once; known
references are left alone. Convention enforced by the few-shot examples: the
model draws profiles in the order it consumes them (sweep: profile first, then
path). ADR 0016's consumed-object map now also records LIST params
(`loft.profiles`) and redirects `profile`/`path` references.

## Alternatives considered
- *Model names real sketch ids directly*: rejected - the very fragility
  ADR 0010 eliminated, worse with two or more names.
- *Sub-edge spine selection for sweep*: rejected - face/edge indices from a 4B
  model are unreliable (ADR 0006); the whole path object is the spine, power
  users refine in the GUI.
- *Positive (outward) shell value*: rejected - "2 mm walls" must not grow the
  part's outer dimensions. (Real-FreeCAD behaviour to be confirmed at the
  cumulative test session - flagged in the test guide.)
- *Bezier/spline profiles*: long tail -> free Python (ADR 0020 boundary).

## Consequences
- Schema v0.7.0 (20 catalog entries); engine+addon 0.12.5-phase7; protocol
  untouched (0.2.0).
- Brain: one compressed rule block + three compact few-shot examples (hexagon
  prism, square-to-circle loft, shell); questions.py defaults extended
  (shell.thickness, polygon sides, slot length).
- Mock: ArcOfCircle geometry + toy shapes for Loft/Sweep/Thickness; new
  `tests/test_vocab_b.py` (contract check pins schema 0.7.0); suite 26/26.
- Honest limits (for the guide): lofts need non-intersecting closed profiles;
  a 4B model may need explicit placements spelled out in the request; shell
  direction on real FreeCAD is a watch-point for the test session.
