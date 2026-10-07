"""
executor/vocabulary/ - IMPLEMENTATIONS of the structured vocabulary commands.

The DEFINITION of the commands (names, parameters, schema) is neutral data in
shared/commands.schema.json; HERE lives the code that actually touches FreeCAD.
Adding a command = one entry in the schema + one function here + one line in the
executor's REGISTRY.

Phase 1 implemented `create_box` and `create_cylinder`. Phase 2 adds the rest of
the catalog: `drill_hole`, `extrude`, `chamfer`, `fillet`, `boolean`. The Phase 2
commands often reference EXISTING objects/edges (see _common.py). Phase 3 adds the
assembly commands: `place_on`/`mate` (placement-by-reference joints, they read a
part's real face), `group` (body hierarchy) and `label` (MBD annotation).

Vocabulary function convention:
    def command(doc, params: dict) -> list:  # returns the created/modified objects
They are called ALREADY inside an undoable transaction (see transaction.py) and
ALREADY on the Qt main thread (see qt_invoker). They must not open transactions
nor worry about threading.
"""

from .create_box import create_box
from .create_cylinder import create_cylinder
from .create_cone import create_cone
from .create_sphere import create_sphere
from .create_torus import create_torus
from .create_sketch import create_sketch
from .sketch_on_face import sketch_on_face
from .drill_hole import drill_hole
from .extrude import extrude
from .revolve import revolve
from .loft import loft
from .sweep import sweep
from .shell import shell
from .chamfer import chamfer
from .fillet import fillet
from .boolean import boolean
from .move import move
from .rotate import rotate
from .mirror import mirror
from .array import array
from .place_on import place_on
from .mate import mate
from .group import group
from .label import label

__all__ = [
    "create_box", "create_cylinder", "create_cone", "create_sphere",
    "create_torus", "create_sketch", "sketch_on_face", "drill_hole", "extrude",
    "revolve", "loft", "sweep", "shell", "chamfer", "fillet", "boolean",
    "move", "rotate", "mirror", "array",
    "place_on", "mate", "group", "label",
]
