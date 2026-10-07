"""
executor/vocabulary/group.py - body hierarchy: collect parts into a named group
(phase 3: placement & assembly).

Command: shared/commands.schema.json -> catalog.group
  params: name (req); members (req; ids of objects to include).

A group is how an assembly is organised in the model tree: the "body hierarchy"
of phase 3. It creates/extends a Part::Group so the parts hang under one named
folder instead of lying loose in the document. Existing members are preserved;
new ones are appended. `name` may repeat an existing group to add to it.
"""

from __future__ import annotations

from typing import List

from ._common import resolve_object


def group(doc, params: dict) -> List:
    """Create/extend a named Part::Group holding the given members."""
    name = str(params["name"])
    members = params.get("members", []) or []

    existing = doc.getObject(name)
    if existing is None:
        existing = doc.addObject("Part::Group", name)
    name = existing.Name  # addObject may uniquify; getObject returns the real Name

    resolved: List = []
    for m in members:
        obj = resolve_object(doc, m)
        if obj is None:
            raise ValueError(f"member '{m}' not found")
        resolved.append(obj)

    current = list(getattr(existing, "Group", []) or [])
    for obj in resolved:
        if obj not in current:
            current.append(obj)
    existing.Group = current
    return [existing]
