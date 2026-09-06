# FreeCAD Agent

A **local-AI copilot** for [FreeCAD](https://www.freecad.org/): type what you want in plain
English and it performs real modeling actions inside FreeCAD — from a single operation to a
whole multi-step component. It runs on a **local** language model via Ollama (nothing leaves
your machine) and sits on top of an **unmodified** FreeCAD: it is an add-on, not a fork.

> **Status: MVP (v0.13.0).** It works end-to-end and has been validated on real multi-step
> parts: it now builds a whole part described in one paragraph, asks when a crucial value is
> missing, and remembers the session. It is still an early, non-commercial hobby/research
> project. Read the [Limits](#limits) section before using it — honesty first.

**[▶ Watch the demo](https://dai.ly/xamziuq)** *(2½ minutes, wait times trimmed — also available as
[mp4 in this repo](docs/media/demo.mp4))*

## What it does

You write, for example:

    create a box 50x50x12 and drill a 10 mm hole in the centre

and the agent perceives the document, plans the steps with the local model, and executes them
as structured CAD commands inside **undoable transactions** — `Ctrl+Z` reverts any action.

The structured vocabulary today covers **20 commands** on the Part workbench: create box,
create cylinder, create cone (and truncated cone), create sphere, create torus, create sketch
(rectangle, circle, regular polygon, rounded slot, closed polyline), sketch on face, extrude,
pocket, revolve, loft, sweep, shell (hollow a solid), drill hole, boolean
(union/difference/intersection), fillet, chamfer, move, rotate, mirror, array (linear and
polar). Fragile decisions (which edges to fillet, which face to sketch on, which object a
follow-up step refers to) are resolved by the executor from the real geometry, not guessed by
the model.

When the vocabulary is not enough, the agent can fall back to **free Python** — but it always
**shows you the exact code first** (transparency), and it too runs inside an undoable
transaction.

Everything starts from inside FreeCAD: open the panel, click **Connect**, and the local AI
engine starts by itself in the background. No terminal, no separate installs — the engine is
pure Python standard library and runs on the interpreter shipped with FreeCAD.

## Build a whole part from one paragraph

Describe the part; the agent splits it into features (base, pockets, holes, rounds…), builds
them **one at a time re-reading the real geometry in between**, and rolls back + replans a
failed feature before moving on. Each feature is one undo step (`Ctrl+Z`).

    Make a mounting bracket: a base plate 60x40x10, cut a rectangular pocket 30x20 and 5 mm
    deep into its top face, drill two 5 mm holes at positions [10, 20] and [50, 20], and
    chamfer the vertical edges by 1.

Small local models have honest limits: keep it under ~6–8 features and give explicit
dimensions/positions (see [Limits](#limits)).

## The agent asks when unsure

If a crucial value is missing ("drill a hole" — which diameter?), a card appears in the panel
with a proposed default: click an option, type a value, or accept the default. Never a
permission prompt, never a pop-up, at most 3 questions per request. Turn the **"Ask me when
unsure"** checkbox off for the old silent behaviour. The questions are generated
deterministically by the engine, so they work with **any** model; a capable model can also ask
its own.

## Session memory

The engine keeps a compressed summary of what you asked earlier in the session
("created Cylinder…"), so a follow-up like "now drill a hole in its centre" resolves to the
right object. The memory lives in **RAM only** and dies with the engine — nothing is stored on
disk.

## Requirements

- **FreeCAD 1.1 or newer** (developed and validated on 1.1.1).
- **[Ollama](https://ollama.com/)** with a local model, for natural language. Optional but
  recommended: without it, the structured command composer in the panel still works. Tested
  with a small model (`qwen3:4b`, about 3.4 GB); larger models give better plans.
- OS: **tested on Windows**. The code is cross-platform (macOS/Linux untested in the wild —
  testers very welcome, see Limits).

## Install

**Via the FreeCAD Addon Manager** (once the addon is listed): Tools > Addon Manager, search
for "FreeCAD Agent", install, restart FreeCAD.

**Manual install (Windows):** download or clone this repository, then double-click
`INSTALL_ADDON.bat`. It copies the addon into your FreeCAD `Mod` folder and prints the
installed versions. Restart FreeCAD afterwards.

**Manual install (macOS/Linux, experimental):** copy the whole repository folder into your
FreeCAD `Mod` directory (e.g. `~/.local/share/FreeCAD/Mod/FreeCADAgent` on Linux,
`~/Library/Application Support/FreeCAD/Mod/FreeCADAgent` on macOS), then restart FreeCAD.

## Quick start

1. In FreeCAD, pick the **FreeCAD Agent** workbench from the workbench selector. The panel
   opens (or use the toolbar button).
2. Click **Connect**. The AI engine starts automatically in the background; the panel shows
   `Engine: running` and the engine log is one click away (**Show engine log**).
3. If Ollama is installed but not running, the engine starts it for you. If Ollama is absent,
   natural language is politely refused but structured commands keep working.
4. Type a request in plain English and press **Ask the agent**. Watch the log: the agent
   reports what it perceives, plans and executes. `Ctrl+Z` undoes any step.
5. Long inference on slow hardware? The panel shows progress and a **Cancel** button; you can
   also opt into a time limit.

For debugging you can start the engine manually with `START_ENGINE.bat` and tick "attach to a
manually-started engine" in the panel.

## Limits

This is an MVP and the limits are stated openly — please read them:

- **Results depend on the model.** Tested with a small local model (`qwen3:4b`); complex
  requests can produce wrong plans. There is bounded self-correction (max 2 repair attempts),
  not magic. Larger models behave better: the design adapts, it does not exclude.
- **It can be slow on weak hardware.** Local inference may take seconds to minutes per step.
- **Tested on Windows only** so far. The code is cross-platform but macOS/Linux are untested
  in the wild: support is experimental and feedback is very welcome.
- **Ollama is required for natural language** (separate, free install). Structured commands
  from the panel work without it.
- **20 structured commands** (Part workbench). Anything else goes through the free-Python
  channel, always shown to you before execution.
- **Phrases that require the model to compute coordinates** (e.g. "holes equally distributed
  on a radius") are beyond small models: give explicit positions, or use the polar `array`.
- **Beyond ~6–8 features per request** a small model drifts; and whether the model itself
  raises a question is model-dependent (the engine's deterministic questions always work).
- **Not for production or safety-critical parts.** It is an assistant you supervise: always
  review the result.
- **MVP:** no cloud adapters yet, no guarantees; APIs and behavior may change.

## How it works

Three separate layers, connected by a local bridge (TCP on 127.0.0.1, token-authenticated,
JSON-RPC 2.0):

1. `addon/` — lives inside FreeCAD (Python 3.11): panel UI, perception of the document, and
   execution of validated commands inside undoable transactions.
2. `engine/` — the AI engine, a separate local process (pure standard library, zero
   dependencies): orchestrates perceive > plan > act > repair and talks to the model.
3. The model — external and interchangeable: today a local model via Ollama's HTTP API.

Shared contracts (command vocabulary, context format, bridge protocol) live in `shared/` as
JSON Schema files. Design decisions are recorded in `docs/adr/` (ADR 0001-0021).

Privacy: no telemetry, no cloud calls, nothing leaves your machine. The panel carries a fixed
**Local AI** badge so you always know inference happens on your machine.

## Running the tests

`RUN_ALL_TESTS.bat` (Windows) runs the whole headless suite — 26 test modules, no FreeCAD or
Ollama needed. On any OS: run the `tests/test_*.py` files with any Python 3.10+.

## Feedback and contributing

Bug reports, feedback and pull requests are welcome — especially **macOS/Linux test
reports**. Please open an issue with your OS, FreeCAD version, the phrase you typed and the
panel/engine log.

## License

[LGPL-2.1-or-later](LICENSE) — consistent with FreeCAD's license.
