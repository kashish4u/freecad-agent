"""
engine/questions.py - deterministic question generator (ADR 0018).

Level 1 of the "double-level interlocution" (Phase 7, star 4): when the model
proposes a command that the validator would REJECT only because a required
parameter is missing (or an enum parameter has an invalid value), the engine can
ASK THE USER instead of silently dropping the action. The question is built here
from a TEMPLATE: zero inference, works with any model (realism on small local
models - principle 9).

Golden rule (from the Phase 7 plan): every question carries a DEFAULT the user
can accept with one click. Consequently a parameter with NO sensible default
(object references like 'target'/'a'/'b') never produces a question: the action
is dropped exactly as in v0.12. Clarifications are NOT permission requests
(principle 4 is untouched): the agent still never asks permission to act.

Pure stdlib, no FreeCAD, fully unit-testable headless.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Sensible per-command defaults for parameters worth asking about (mm/degrees).
# A (cmd, param) pair MISSING from this table has no default => no question.
# Object references (target/a/b/face/edges) are deliberately absent: no default
# can exist for "which object did you mean" at this level.
DEFAULTS: Dict[Tuple[str, str], Any] = {
    ("create_box", "length"): 10,
    ("create_box", "width"): 10,
    ("create_box", "height"): 10,
    ("create_cylinder", "radius"): 5,
    ("create_cylinder", "height"): 10,
    ("drill_hole", "diameter"): 6,
    ("drill_hole", "depth"): 10,
    ("create_sketch", "shape"): "rectangle",
    ("create_sketch", "width"): 20,
    ("create_sketch", "height"): 10,
    ("create_sketch", "radius"): 10,
    ("sketch_on_face", "shape"): "rectangle",
    ("sketch_on_face", "width"): 10,
    ("sketch_on_face", "height"): 10,
    ("sketch_on_face", "radius"): 5,
    ("extrude", "distance"): 10,
    ("create_cone", "radius1"): 5,
    ("create_cone", "height"): 10,
    ("create_sphere", "radius"): 5,
    ("create_torus", "radius1"): 10,
    ("create_torus", "radius2"): 2,
    ("shell", "thickness"): 2,
    ("create_sketch", "sides"): 6,
    ("create_sketch", "length"): 30,
    ("sketch_on_face", "sides"): 6,
    ("sketch_on_face", "length"): 20,
    ("fillet", "radius"): 2,
    ("chamfer", "size"): 1,
    ("rotate", "angle"): 90,
    ("boolean", "op"): "union",
    ("array", "pattern"): "linear",
    ("array", "count"): 4,
    ("array", "spacing"): 20,
}


def default_for(cmd: str, param: str, pspec: Optional[dict] = None) -> Any:
    """
    The proposed default for a parameter, or None if there is none.
    Priority: our DEFAULTS table, then the schema's own 'default', then the
    first enum value (an enum always has an acceptable answer).
    """
    if (cmd, param) in DEFAULTS:
        return DEFAULTS[(cmd, param)]
    if isinstance(pspec, dict):
        if "default" in pspec:
            return pspec["default"]
        enum = pspec.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
    return None


def classify(invocation: dict, catalog) -> Dict[str, Any]:
    """
    Decide whether a command invocation that FAILS validation is FIXABLE by
    asking the user (ADR 0018). Returns:
      {
        "fixable": bool,
        "missing": [param, ...],          # required params absent, all with defaults
        "bad_enum": {param: [allowed]},   # enum params with an invalid value
        "other": [error, ...],            # anything else (=> not fixable)
      }
    Fixable = at least one missing/bad_enum entry AND no 'other' blocking
    problem AND every missing param has a default to propose. Pure.
    """
    out: Dict[str, Any] = {"fixable": False, "missing": [],
                           "bad_enum": {}, "other": []}
    if not isinstance(invocation, dict):
        out["other"].append("invocation is not an object")
        return out
    cmd = invocation.get("cmd")
    params = invocation.get("params", {})
    spec = catalog.spec(cmd) if isinstance(cmd, str) else None
    if spec is None:
        out["other"].append(f"unknown command '{cmd}'")
        return out
    if not isinstance(params, dict):
        out["other"].append("'params' is not an object")
        return out

    pschema = spec.get("params", {})
    required = pschema.get("required", [])
    properties = pschema.get("properties", {})

    for req in required:
        if req not in params:
            out["missing"].append(req)

    for name, value in params.items():
        pspec = properties.get(name)
        if pspec is None:
            continue  # extra param: non-blocking warning, executor ignores it
        jtype = pspec.get("type")
        enum = pspec.get("enum")
        if enum is not None and value not in enum:
            out["bad_enum"][name] = list(enum)
            continue
        # Any other violation (wrong type, minimum, bad array item) is not a
        # question we can template a safe default for: leave it to the classic
        # drop + self-correction path.
        if jtype and not _type_ok(value, jtype):
            out["other"].append(f"parameter '{name}': wrong type")
        elif jtype in ("number", "integer") and "minimum" in pspec \
                and isinstance(value, (int, float)) \
                and not isinstance(value, bool) and value < pspec["minimum"]:
            out["other"].append(f"parameter '{name}': below minimum")

    if out["other"]:
        return out
    # Every missing param must have a default to propose (golden rule).
    for p in out["missing"]:
        if default_for(cmd, p, properties.get(p)) is None:
            out["other"].append(f"parameter '{p}': no default to propose")
            return out
    out["fixable"] = bool(out["missing"] or out["bad_enum"])
    return out


def build_question(cmd: str, param: str, catalog,
                   bad_value: Any = None) -> Optional[dict]:
    """
    Build the templated question payload for ONE parameter of ONE command.
    Returns None when no default exists (then there is nothing to ask).
    Shape (sent to the add-on via user.question, minus the ids the engine adds):
      {"cmd", "param", "question", "options": [...], "default": <value>}
    """
    spec = catalog.spec(cmd) or {}
    properties = spec.get("params", {}).get("properties", {})
    pspec = properties.get(param, {}) or {}
    default = default_for(cmd, param, pspec)
    if default is None:
        return None
    enum = pspec.get("enum")
    if isinstance(enum, list) and enum:
        options = [str(v) for v in enum]
        if bad_value is not None:
            question = (f"'{param}' of '{cmd}' must be one of "
                        f"{', '.join(options)} - '{bad_value}' is not valid. "
                        f"I propose '{default}'.")
        else:
            question = (f"'{cmd}' needs '{param}' (one of "
                        f"{', '.join(options)}). I propose '{default}'.")
    else:
        unit = _unit_hint(pspec, param)
        options = [str(default)]
        question = (f"'{cmd}' needs a value for '{param}'. "
                    f"I propose {default}{unit}.")
    return {"cmd": cmd, "param": param, "question": question,
            "options": options, "default": default}


def coerce_answer(value: Any, cmd: str, param: str, catalog) -> Any:
    """
    Convert a user's answer (often a string typed in the panel's free field)
    to the type the parameter expects. Best-effort: on any failure the raw
    value is returned and normal validation judges it downstream (principle 7).
    """
    spec = catalog.spec(cmd) or {}
    pspec = spec.get("params", {}).get("properties", {}).get(param, {}) or {}
    jtype = pspec.get("type")
    try:
        if jtype == "number" and isinstance(value, str):
            return float(value.replace(",", "."))
        if jtype == "integer" and isinstance(value, str):
            return int(float(value))
        if jtype == "array" and isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",")
                     if p.strip()]
            return [float(p) for p in parts]
        if jtype == "boolean" and isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
    except (TypeError, ValueError):
        return value
    return value


def _unit_hint(pspec: dict, param: str) -> str:
    """' mm'/' degrees' suffix for the question text, when obvious."""
    if pspec.get("type") not in ("number", "integer"):
        return ""
    name = param.lower()
    if "angle" in name:
        return " degrees"
    if name in ("count",):
        return ""
    return " mm"


def _type_ok(value: Any, json_type: str) -> bool:
    """Same minimal type check as fake_brain (kept local: no circular import)."""
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    return True
