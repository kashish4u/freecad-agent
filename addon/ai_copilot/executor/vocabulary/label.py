"""
executor/vocabulary/label.py - MBD annotation: name a part and attach a note
(Machine-Body-Data: the readable metadata an assembly must carry so it is
interpretable, not just geometry). Phase 3.

Command: shared/commands.schema.json -> catalog.label
  params: target (req); label (req; the part's name/label); note (optional).

Naming is the first half of MBD: every part in an assembly should have a clear
.Label so the assembly tree, drawings, and downstream tools can address it. An
optional note is stored as a DocString property on the object.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object


def label(doc, params: dict) -> List:
    """Set `target`'s label (and optional note) for the assembly tree."""
    obj = resolve_object(doc, params["target"])
    if obj is None:
        raise ValueError(f"target '{params['target']}' not found")

    obj.Label = str(params["label"])
    if params.get("note") is not None:
        try:
            obj.addProperty("DocString", "Note", "Additional", str(params["note"]))
        except Exception:
            # Not all object types support custom properties; the name itself is
            # the primary MBD signal, so a failed note is not fatal.
            pass
    return [obj]
