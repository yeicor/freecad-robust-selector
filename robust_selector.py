"""Public scripting API for Robust Selector.

This module intentionally has no FreeCADGui dependency, so selectors can be
constructed and evaluated from FreeCAD's Python console or from automation code.

Architectural Scope Boundary (MIN-4):
FreeCAD FeatureSelector is strictly focused on robust semantic geometric selection
and transparent property binding.
Strict boundaries: Strictly forbids mesh generation, direct modeling/booleans,
or non-native dependencies outside FreeCAD / PySide / OCC.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fs_freecad import FeatureRef, add_selection
from fs_selector import Selector, Step

__version__ = "0.6.0"


def selector(kind: str, steps: Iterable[Step] = (), *, expected_count: Optional[int] = None, name: str = "") -> Selector:
    """Build a selector query without exposing transient TopoShape indices as intent."""
    return Selector(kind, tuple(steps), expected_count=expected_count, name=name)


def resolve(obj, query: Selector) -> list[FeatureRef]:
    """Evaluate *query* against the object's current shape."""
    return query.evaluate(obj)


def apply(obj, query: Selector, *, clear: bool = True) -> list[FeatureRef]:
    """Evaluate a query and hand the result to FreeCAD's native selection API."""
    refs = resolve(obj, query)
    if query.expected_count is not None and len(refs) != query.expected_count:
        return []
    return add_selection(refs, clear=clear)


def create(doc, source_obj, query: Selector, *, label: Optional[str] = None, captured_selection: Iterable[str] = ()):
    """Create an explicit persistent selector object in *doc*."""
    from fs_document import create_selector_object
    obj = create_selector_object(doc, source_obj, query, label=label, captured_selection=captured_selection)
    doc.recompute()
    return obj


def bind(selector_obj, target_obj, prop_name: str):
    """Explicitly bind a persistent selector to one supported native property."""
    from fs_bindings import bind_selector, property_mode, read_property_value
    mode = property_mode(target_obj, prop_name)
    if mode is None:
        raise ValueError(f"Unsupported native property: {prop_name}")
    record = bind_selector(selector_obj, target_obj, prop_name, mode, read_property_value(target_obj, prop_name))
    selector_obj.Document.recompute()
    return record


__all__ = ["Selector", "Step", "FeatureRef", "selector", "resolve", "apply", "create", "bind", "__version__"]
