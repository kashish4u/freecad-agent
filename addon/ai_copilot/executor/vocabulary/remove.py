"""
executor/vocabulary/remove.py - implementation of the `remove` command.

Command definition: shared/commands.schema.json -> catalog.remove
  params: target (req) -- id/label/name of the object (or feature) to remove.

Removes a document object. In the Part workbench a feature (fillet, chamfer,
shell, boolean, ...) is itself a separate object with its own .Name, so this one
command covers both "remove an object" and "remove a feature". Bare sub-elements
(edges/faces) are NOT objects and cannot be removed directly; the engine resolves
"remove edge X" to the feature owning that edge before calling this command.

Returns [] -- nothing is created; the object is deleted. The removed id is
available to the engine from the invocation's `target`.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object  # noqa: F401  (re-exported for convenience)


def remove(doc, params: dict) -> List:
    """Remove a document object (feature or primitive) by id/label/name."""
    import FreeCAD  # lazy import: available only inside FreeCAD.

    target_id = params.get("target")
    if not target_id or not str(target_id).strip():
        raise ValueError("remove: 'target' is required (id/label/name of the object)")

    target = resolve_object(doc, str(target_id))
    name = target.Name
    doc.removeObject(name)
    return []
