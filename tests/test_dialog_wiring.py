"""A method called on self must exist on the class that calls it.

The passthrough window is several worker threads plus the dialog, and they look alike from
the inside - every one of them says ``self.share``, ``self.line``, ``self.install``. A
method written into the wrong one of them is invisible: it imports, it starts, the tests
pass, and it fails with AttributeError only when that branch is finally reached. That is
exactly what happened to the join - it sat on the dialog while its only caller was on the
worker thread, so the very last step of a long job could never run, hours in.

This reads the module rather than importing it, and only complains about names the file
itself defines as methods somewhere - so Qt's own attributes and anything assigned in
__init__ are left alone.
"""
from __future__ import annotations

import ast
import pathlib

MODULES = ["passthrough_dialog.py", "pick_dialog.py", "app.py", "timeline.py"]


def _classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _methods_of(name: str, classes: dict[str, ast.ClassDef], seen=None) -> set[str]:
    """Every method a class has, following the bases that live in the same file."""
    seen = seen or set()
    if name in seen or name not in classes:
        return set()
    seen.add(name)
    node = classes[name]
    mine = {n.name for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for base in node.bases:
        if isinstance(base, ast.Name):
            mine |= _methods_of(base.id, classes, seen)
    return mine


def _calls_on_self(node: ast.ClassDef) -> set[str]:
    found = set()
    for inner in ast.walk(node):
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "self"):
            found.add(inner.func.attr)
    return found


def test_every_method_called_on_self_is_on_that_class():
    here = pathlib.Path(__file__).resolve().parent.parent / "blueshelf"
    wrong: list[str] = []
    for name in MODULES:
        path = here / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = _classes(tree)
        # Names the file defines as a method on any class. A name it never defines is
        # something inherited from Qt or set on the instance, and none of our business.
        defined_somewhere = set()
        for other in classes.values():
            defined_somewhere |= {n.name for n in other.body
                                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for cls_name, node in classes.items():
            has = _methods_of(cls_name, classes)
            for called in sorted(_calls_on_self(node)):
                if called in defined_somewhere and called not in has:
                    owners = [o for o, c in classes.items()
                              if called in {n.name for n in c.body
                                            if isinstance(n, (ast.FunctionDef,
                                                              ast.AsyncFunctionDef))}]
                    wrong.append(f"{name}: {cls_name} calls self.{called}(), "
                                 f"but that is defined on {', '.join(owners)}")
    assert not wrong, "a method lives on the wrong class:\n  " + "\n  ".join(wrong)
