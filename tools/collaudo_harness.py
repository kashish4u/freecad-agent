# -*- coding: utf-8 -*-
"""
tools/collaudo_harness.py - headless integration harness for Phase 7 testing.

WHAT IT IS
  Runs the REAL agent pipeline end to end WITHOUT the FreeCAD GUI: the real
  engine (bridge_server, launched from THIS repo - so it exercises the current
  source, no re-install needed), the real local Ollama model, and REAL FreeCAD
  geometry via FreeCADCmd. It types each test phrase through user.prompt exactly
  like the panel would, auto-answers any clarification question with the proposed
  default, runs NUMERIC checks on the resulting document, and writes a results
  file that can be reviewed offline.

  It replaces the manual "type a phrase, read the log, eyeball the shape" loop for
  the geometry-heavy sections (vocabulary V/W and pilots P). It does NOT test the
  panel UI itself (question cards, Cancel button, checkboxes) - those stay a human
  test.

HOW TO RUN (on the machine that has FreeCAD 1.1 + Ollama with the model pulled):

    "C:\\Program Files\\FreeCAD 1.1\\bin\\FreeCADCmd.exe" tools\\collaudo_harness.py

  Optional: choose which set to run with an environment variable
    set FCA_SET=vocab     &  ... FreeCADCmd ...   (V + W only)
    set FCA_SET=pilot     &  ... FreeCADCmd ...   (P only)
    set FCA_SET=all       &  ... FreeCADCmd ...   (smoke + V + W + P)  [default]

  Results are written to:  tools\\collaudo_out\\results.md   (+ results.json)

DESIGN NOTES
  - The add-on side (BridgeClient) is reused as-is. FreeCAD APIs are not thread
    safe, so incoming handlers (command.execute, perception.*, agent.status,
    user.question) are marshalled onto the MAIN thread through a tiny queue
    invoker that the main thread pumps - the headless equivalent of qt_invoker.
  - Test phrases run on a WORKER thread (send_user_prompt blocks until the whole
    plan finishes, and must not run on the "main" thread that serves callbacks).
  - Every clarification question is auto-answered with the proposed default (a
    declared default is a PASS on a small local model, per the test guide).
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback


# --------------------------------------------------------------------------- #
#  Locate the repo and wire sys.path exactly like InitGui.py does.
# --------------------------------------------------------------------------- #
def _repo_root():
    for cand in (globals().get("__file__"), sys.argv[0] if sys.argv else None):
        if cand:
            here = os.path.dirname(os.path.abspath(cand))
            # this file is <root>/tools/collaudo_harness.py
            root = os.path.dirname(here)
            if os.path.isdir(os.path.join(root, "addon")) and \
               os.path.isdir(os.path.join(root, "shared")):
                return root
    # last resort: current working directory
    cwd = os.path.abspath(os.getcwd())
    if os.path.isdir(os.path.join(cwd, "addon")):
        return cwd
    raise RuntimeError("cannot locate the repo root (addon/ + shared/)")


ROOT = _repo_root()
for _p in (os.path.join(ROOT, "addon"), os.path.join(ROOT, "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import FreeCAD  # noqa: E402  (provided by FreeCADCmd)
from ai_copilot import executor as _executor  # noqa: E402
from ai_copilot.bridge_client import BridgeClient  # noqa: E402

# Capture every command.execute the engine drives, with the EXACT params the
# model produced and the result (ok/error). Invaluable for diagnosing a failed
# feature (e.g. a loft whose profiles the model forgot to offset). The current
# case's trace is collected in this module-level list, reset per case.
_EXEC_TRACE = []
_orig_execute = _executor.execute


def _traced_execute(invocation):
    result = _orig_execute(invocation)
    try:
        _EXEC_TRACE.append({
            "cmd": invocation.get("cmd"),
            "params": invocation.get("params"),
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "created_ids": result.get("created_ids", []),
        })
    except Exception:
        pass
    return result


_executor.execute = _traced_execute


# --------------------------------------------------------------------------- #
#  Main-thread invoker (headless replacement for qt_invoker.MainThreadInvoker).
# --------------------------------------------------------------------------- #
class QueueInvoker:
    """Marshal handler calls onto the main thread via a queue the main pumps."""

    def __init__(self):
        self._q = queue.Queue()

    def invoke(self, fn, params, timeout=120.0):
        done = threading.Event()
        box = {}

        def task():
            try:
                box["result"] = fn(params)
            except BaseException as exc:  # re-raised on the caller side
                box["error"] = exc
            finally:
                done.set()

        self._q.put(task)
        if not done.wait(timeout):
            raise TimeoutError(f"main-thread task exceeded {timeout}s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def pump(self, stop_event, idle=0.05):
        """Run queued tasks on the calling (main) thread until stop_event is set."""
        while not stop_event.is_set():
            try:
                task = self._q.get(timeout=idle)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:
                pass  # a task captures its own errors; never kill the pump
        # drain whatever is left so no worker blocks forever
        while True:
            try:
                self._q.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Numeric inspection helpers (run on the main thread).
# --------------------------------------------------------------------------- #
def _visible_solids(doc):
    """
    Final RESULT solids in the document. Headless FreeCADCmd has no ViewObject,
    so visibility is unreliable; instead we report only objects NOT consumed by a
    parent - an object with an empty InList is a top-level result (e.g. the
    `Drilled` Cut, not the `Box`/`DrillTool` it swallowed; the `Thickness` shell,
    not the original box). This is what actually needs measuring.
    """
    out = []
    for o in doc.Objects:
        shp = getattr(o, "Shape", None)
        if shp is None:
            continue
        try:
            solids = shp.Solids
        except Exception:
            solids = []
        if not solids:
            continue
        # Consumed by a parent feature (Cut/Fusion/Thickness/Loft...) => skip.
        if len(getattr(o, "InList", []) or []) != 0:
            continue
        bb = shp.BoundBox
        out.append({
            "name": o.Name,
            "type": o.TypeId,
            "dims": [round(bb.XLength, 2), round(bb.YLength, 2),
                     round(bb.ZLength, 2)],
            "volume": round(float(shp.Volume), 1),
            "solids": len(solids),
        })
    return out


def inspect_doc(_params):
    doc = FreeCAD.ActiveDocument
    if doc is None:
        return {"objects": 0, "visible_solids": []}
    return {
        "objects": len(doc.Objects),
        "object_names": [o.Name for o in doc.Objects],
        "visible_solids": _visible_solids(doc),
    }


def new_document(params):
    name = params.get("name", "Harness")
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(doc.Name)
    return doc.Name


# --------------------------------------------------------------------------- #
#  Verdict functions per test case. Each gets the inspection dict and returns
#  (verdict, note). Kept LENIENT: the model is nondeterministic; we hard-FAIL
#  only on clear breakage and on the shell watch-point.
# --------------------------------------------------------------------------- #
def _biggest(solids):
    return max(solids, key=lambda s: s["volume"]) if solids else None


def v_any_solid(insp, actions=None):
    s = insp["visible_solids"]
    if not s:
        return "FAIL", "no visible solid produced"
    b = _biggest(s)
    return "PASS", f"solid {b['name']} dims={b['dims']} vol={b['volume']}"


def v_pilot(insp, actions=None):
    """
    Strict verdict for a whole-part pilot: it must end as ONE integrated solid
    and no action may have failed (even a 'recovered' one leaves a wrong body).
    """
    s = insp["visible_solids"]
    failed = [a for a in (actions or []) if not a.get("ok")]
    if not s:
        return "FAIL", "no solid produced"
    if len(s) > 1:
        names = ", ".join(f"{x['name']}({x['volume']})" for x in s)
        return "FAIL", (f"{len(s)} DISJOINT solids [{names}] - the part did not "
                        f"integrate into one body (stale/consumed reference?)")
    if failed:
        f = failed[0]
        return "REVIEW", (f"one solid, but an action FAILED: {f.get('cmd')} -> "
                          f"{f.get('error', '')[:70]}")
    b = s[0]
    return "PASS", f"single solid {b['name']} dims={b['dims']} vol={b['volume']}"


def v_shell_watchpoint(insp, actions=None):
    """W6: box 40x30x20 hollowed with 2mm walls, open top -> walls INWARD."""
    s = insp["visible_solids"]
    if not s:
        return "FAIL", "no solid produced by the shell step"
    b = _biggest(s)
    dx, dy, dz = b["dims"]
    outer_ok = (abs(dx - 40) <= 1 and abs(dy - 30) <= 1 and abs(dz - 20) <= 1)
    grew_out = (abs(dx - 44) <= 1 and abs(dy - 34) <= 1 and abs(dz - 24) <= 1)
    full = 40 * 30 * 20
    hollow = b["volume"] < 0.7 * full  # a hollow box has much less volume
    if grew_out:
        return "FAIL", (f"WALLS GREW OUTWARD dims={b['dims']} - flip the "
                        f"thickness sign in shell.py (ADR 0021 watch-point)")
    if outer_ok and hollow:
        return "PASS", f"outer {b['dims']} kept, hollow (vol={b['volume']}) - inward OK"
    if outer_ok and not hollow:
        return "REVIEW", (f"outer {b['dims']} OK but volume {b['volume']} looks "
                          f"solid - check it is actually hollowed")
    return "REVIEW", f"unexpected dims {b['dims']} vol={b['volume']} - eyeball this"


def v_dims_near(target, tol=1.5):
    def check(insp, actions=None):
        s = insp["visible_solids"]
        if not s:
            return "FAIL", "no visible solid produced"
        b = _biggest(s)
        d = b["dims"]
        ok = all(abs(d[i] - target[i]) <= tol for i in range(3))
        verdict = "PASS" if ok else "REVIEW"
        return verdict, f"{b['name']} dims={d} (target {target}) vol={b['volume']}"
    return check


# --------------------------------------------------------------------------- #
#  Test cases.  phrase + optional dims target + verdict fn.
# --------------------------------------------------------------------------- #
SMOKE = [
    dict(id="S1", phrase="create a box 30x20x15", verdict=v_dims_near([30, 20, 15])),
    dict(id="S2", phrase="create a box 40x40x10 and drill a 8 mm hole in the centre",
         verdict=v_any_solid),
]

VOCAB = [
    dict(id="V1", phrase="create a cone with base radius 10 and height 25",
         verdict=v_any_solid),
    dict(id="V2", phrase="create a truncated cone, base radius 10, top radius 4, height 20",
         verdict=v_any_solid),
    dict(id="V3", phrase="create a sphere of radius 12", verdict=v_any_solid),
    dict(id="V4", phrase="create a torus, ring radius 20, tube radius 3",
         verdict=v_any_solid),
    dict(id="V5", phrase="draw a 20x10 rectangle and revolve it around the X axis",
         verdict=v_any_solid),
    dict(id="V6", phrase="draw a 15x8 rectangle and revolve it 180 degrees around the Y axis",
         verdict=v_any_solid),
    dict(id="W1", phrase="make a hexagonal prism, radius 15, 8 mm tall",
         verdict=v_any_solid),
    dict(id="W2", phrase="draw a slot 30 mm long and 10 mm wide and extrude it 5 mm",
         verdict=v_any_solid),
    dict(id="W3", phrase="draw a closed polyline through [0,0], [40,0], [40,20], [0,20] and extrude it 6 mm",
         verdict=v_dims_near([40, 20, 6])),
    dict(id="W4", phrase="loft a 40x40 square into a circle of radius 10, 30 mm above it",
         verdict=v_any_solid),
    dict(id="W5", phrase="draw a circle of radius 4 on the XZ plane, then a polyline path through [0,0], [50,0], [50,40], and sweep the circle along the path",
         verdict=v_any_solid),
    dict(id="W6", phrase="create a box 40x30x20, then hollow it out with 2 mm walls, open on top",
         verdict=v_shell_watchpoint),
]

PILOT = [
    dict(id="P1", phrase=(
        "Make a circular flange: a disc of radius 40 and thickness 8, with a "
        "centred hole of diameter 20, and 4 bolt holes of diameter 6 arranged "
        "in a circle of radius 30 around the centre, and round the top edges "
        "with radius 1."), verdict=v_pilot, budget=600),
    dict(id="P2", phrase=(
        "Make a mounting bracket: a base plate 60x40x10, cut a rectangular "
        "pocket 30x20 and 5 mm deep into its top face, drill two 5 mm holes at "
        "positions [10, 20] and [50, 20], and chamfer the vertical edges by 1."),
        verdict=v_pilot, budget=600),
    dict(id="P3", phrase=(
        "Make a support stand: a base plate 80x80x6, a central boss cylinder of "
        "radius 15 and height 30 on top of it, drill a 10 mm hole through the "
        "boss, and arrange 8 holes of 4 mm on a circle of radius 32 around the "
        "centre of the plate."), verdict=v_pilot, budget=600),
]


def _select_cases():
    which = os.environ.get("FCA_SET", "all").lower()
    if which == "vocab":
        return VOCAB
    if which == "pilot":
        return PILOT
    if which == "smoke":
        return SMOKE
    if which == "w4":
        return [c for c in VOCAB if c["id"] == "W4"]
    if which == "pilot_w4":
        # re-run the one loft failure (with the new action trace) + the 3 pilots.
        return [c for c in VOCAB if c["id"] == "W4"] + PILOT
    return SMOKE + VOCAB + PILOT


# --------------------------------------------------------------------------- #
#  Harness driver.
# --------------------------------------------------------------------------- #
class Harness:
    def __init__(self):
        self.invoker = QueueInvoker()
        self.log_lines = []
        self.status_lines = []           # agent.status for the CURRENT case
        self._answered = []              # questions auto-answered this case
        self.client = BridgeClient(
            invoker=self.invoker,
            logger=self._log,
            on_status=self._on_status,
            on_question=self._on_question,
        )

    # -- callbacks ----------------------------------------------------------
    def _log(self, msg):
        self.log_lines.append(str(msg))

    def _on_status(self, params):
        self.status_lines.append(f"{params.get('phase')}: {params.get('message')}")

    def _on_question(self, params):
        # Auto-answer with the proposed default, on a worker thread (send_user_
        # answer is a blocking call that must not run on the main/pump thread).
        qid = params.get("question_id")
        self._answered.append(params.get("question"))

        def answer():
            time.sleep(0.05)
            try:
                self.client.send_user_answer(qid, use_default=True)
            except Exception as exc:
                self._log(f"auto-answer failed: {exc}")

        threading.Thread(target=answer, daemon=True).start()

    # -- run one case (on the worker thread) --------------------------------
    def run_case(self, case):
        self.status_lines = []
        self._answered = []
        _EXEC_TRACE.clear()
        rec = {"id": case["id"], "phrase": case["phrase"]}
        try:
            self.invoker.invoke(new_document, {"name": "H_" + case["id"]})
            # Per-model-call timeout. A small local 4B on CPU is SLOW (~2-4 min a
            # call); the overall wait must outlast several calls (decompose +
            # one plan per feature + up to 2 repairs), so it is a large multiple.
            budget = case.get("budget", 480)
            t0 = time.time()
            result = self.client.send_user_prompt(
                case["phrase"], ai_timeout=budget, ask_when_unsure=True,
                timeout=budget * 10 + 300)
            rec["seconds"] = round(time.time() - t0, 1)
            rec["accepted"] = bool(result.get("accepted"))
            rec["cancelled"] = bool(result.get("cancelled"))
            rec["summary"] = result.get("summary") or result.get("clarification") \
                or result.get("error") or ""
            insp = self.invoker.invoke(inspect_doc, {})
            rec["inspection"] = insp
            rec["questions_auto_answered"] = list(self._answered)
            verdict, note = case["verdict"](insp, list(_EXEC_TRACE))
            if not rec["accepted"] and verdict == "PASS":
                verdict, note = "FAIL", "engine did not accept the request"
            rec["verdict"] = verdict
            rec["note"] = note
        except Exception as exc:
            rec["verdict"] = "ERROR"
            rec["note"] = f"{type(exc).__name__}: {exc}"
            rec["trace"] = traceback.format_exc()
        rec["status_log"] = list(self.status_lines)
        rec["actions"] = list(_EXEC_TRACE)
        return rec

    def run_all(self, cases, results, done_event):
        try:
            if not self.client.wait_connected(60):
                results.append({"id": "-", "verdict": "ERROR",
                                "note": "engine did not connect within 60s"})
                return
            for case in cases:
                print(f"  [running] {case['id']}: {case['phrase'][:60]}...",
                      flush=True)
                rec = self.run_case(case)
                print(f"    -> {rec['verdict']}: {rec.get('note', '')}", flush=True)
                results.append(rec)
                # Write partial results after EVERY case, so a long run that is
                # interrupted or crashes still leaves everything completed so far.
                try:
                    _write_results(cases, results)
                except Exception:
                    pass
        finally:
            done_event.set()


def _write_results(cases_run, results):
    outdir = os.path.join(ROOT, "tools", "collaudo_out")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "when": stamp,
        "engine_version_expected": "0.12.6-phase7",
        "set": os.environ.get("FCA_SET", "all"),
        "count": len(results),
    }
    with open(os.path.join(outdir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": results}, f, indent=2)

    lines = [f"# Collaudo harness results - {stamp}", "",
             f"Set: `{meta['set']}`  ·  engine expected `0.12.6-phase7`  ·  "
             f"{len(results)} cases", "",
             "| ID | Verdict | Seconds | Q? | Note |",
             "|----|---------|---------|----|------|"]
    for r in results:
        q = "yes" if r.get("questions_auto_answered") else ""
        note = str(r.get("note", "")).replace("|", "/")[:90]
        lines.append(f"| {r.get('id')} | {r.get('verdict')} | "
                     f"{r.get('seconds', '')} | {q} | {note} |")
    lines += ["", "## Per-case detail", ""]
    for r in results:
        lines.append(f"### {r.get('id')} - {r.get('verdict')}")
        lines.append(f"- phrase: `{r.get('phrase', '')}`")
        lines.append(f"- summary: {r.get('summary', '')}")
        insp = r.get("inspection", {})
        for s in insp.get("visible_solids", []):
            lines.append(f"- solid `{s['name']}` {s['type']} dims={s['dims']} "
                         f"vol={s['volume']} solids={s['solids']}")
        if r.get("questions_auto_answered"):
            lines.append(f"- auto-answered (default): {r['questions_auto_answered']}")
        if r.get("actions"):
            lines.append("- actions the model produced:")
            for a in r["actions"]:
                tag = "ok" if a.get("ok") else f"FAIL: {a.get('error', '')}"
                lines.append(f"    - {a.get('cmd')} {a.get('params')} -> {tag}")
        if r.get("status_log"):
            lines.append("- log:")
            for sl in r["status_log"]:
                lines.append(f"    - {sl}")
        if r.get("trace"):
            lines.append("```\n" + r["trace"] + "\n```")
        lines.append("")
    with open(os.path.join(outdir, "results.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return outdir


def main():
    cases = _select_cases()
    print(f"FreeCAD Agent collaudo harness - {len(cases)} cases "
          f"(set={os.environ.get('FCA_SET', 'all')})", flush=True)
    print(f"repo: {ROOT}", flush=True)

    h = Harness()
    results = []
    done = threading.Event()

    # Start the client (managed): it launches the engine from THIS repo and
    # connects back. All handler callbacks are pumped on THIS (main) thread.
    h.client.start(managed=True)

    worker = threading.Thread(target=h.run_all, args=(cases, results, done),
                              name="cases", daemon=True)
    worker.start()

    # Main thread: pump the invoker queue until the worker is done. A global
    # wall-clock safety net stops a wedged run.
    safety_deadline = time.time() + 300 + sum(c.get("budget", 480) * 10 + 300
                                              for c in cases)
    while not done.is_set():
        h.invoker.pump(done, idle=0.05)
        if time.time() > safety_deadline:
            print("  [safety] global deadline reached - stopping.", flush=True)
            done.set()
            break

    try:
        h.client.stop()
    except Exception:
        pass

    outdir = _write_results(cases, results)
    npass = sum(1 for r in results if r.get("verdict") == "PASS")
    print(f"\nDONE. {npass}/{len(results)} PASS. Results in: {outdir}", flush=True)
    # Give the engine a moment to shut down before FreeCADCmd exits.
    time.sleep(1.0)


# FreeCADCmd runs a script file WITHOUT setting __name__ == "__main__", so a
# classic guard would never fire. Invoke main() directly (guarded so any error is
# printed, not silently swallowed). FCA_IMPORT_ONLY=1 lets tests import without
# running.
if os.environ.get("FCA_IMPORT_ONLY") != "1":
    try:
        main()
    except Exception:
        print("HARNESS CRASHED:\n" + traceback.format_exc(), flush=True)
        try:
            _err_dir = os.path.join(ROOT, "tools", "collaudo_out")
            os.makedirs(_err_dir, exist_ok=True)
            with open(os.path.join(_err_dir, "crash.txt"), "w",
                      encoding="utf-8") as _f:
                _f.write(traceback.format_exc())
        except Exception:
            pass
