"""Persistent Feature Selector document objects.

The selector object is an ordinary ``App::FeaturePython`` object.  Its source is
a normal ``App::PropertyLink`` and its native consumers explicitly link back to
the selector through the target's ``RobustSelectionSources`` property.  This
creates a visible FreeCAD dependency edge: source -> robust selector -> target.
No global observer or timer is involved.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from fs_bindings import BINDING_STATUS_PROPERTY, ensure_binding_properties, document_name, object_name
from fs_freecad import selection_pairs
from fs_selector import Selector

def _find_parent_body(obj: Any) -> Any:
    if hasattr(obj, "InList"):
        for parent in obj.InList:
            if parent.isDerivedFrom("PartDesign::Body"):
                return parent
            b = _find_parent_body(parent)
            if b:
                return b
    return None

SELECTOR_GROUP_NAME = "FeatureSelectorRobust"
SELECTOR_GROUP_LABEL = "Robust Selections"


def _set_editor_mode(obj: Any, prop: str, mode: int) -> None:
    obj.setEditorMode(prop, mode)


def _add_property(obj: Any, type_name: str, name: str, group: str, description: str, default: Any = None) -> None:
    existing = set(getattr(obj, "PropertiesList", []) or [])
    if name in existing:
        return
    obj.addProperty(type_name, name, group, description)
    if default is not None:
        setattr(obj, name, default)


def ensure_selector_properties(obj: Any) -> Any:
    """Create/upgrade the public selector properties used by this workbench."""
    _add_property(
        obj,
        "App::PropertyLink",
        "BaseObject",
        "Selection",
        "Source object whose current topology is evaluated",
    )
    _add_property(
        obj,
        "App::PropertyEnumeration",
        "FeatureKind",
        "Selection",
        "Feature family evaluated by this selector",
    )
    if getattr(obj, "FeatureKind", None) in (None, ""):
        obj.FeatureKind = ["Shape", "Vertex", "Edge", "Wire", "Face", "Shell", "Solid", "CompSolid"]
    _add_property(
        obj,
        "App::PropertyString",
        "Query",
        "Selection",
        "Serialized semantic selector query",
    )
    _add_property(
        obj,
        "App::PropertyStringList",
        "CapturedSelection",
        "Selection",
        "Subelement names selected by the user when this selector was created; audit information only",
        [],
    )
    _add_property(
        obj,
        "App::PropertyInteger",
        "ExpectedCount",
        "Selection",
        "Required number of resolved subelements for a binding to be considered valid",
        -1,
    )
    _add_property(
        obj,
        "App::PropertyBool",
        "Enabled",
        "Selection",
        "Enable evaluation of this selector",
        True,
    )
    _add_property(
        obj,
        "App::PropertyString",
        "Resolved",
        "Diagnostics",
        "Current resolved native object/subelement pairs, or a structured error",
        "[]",
    )
    _add_property(
        obj,
        "App::PropertyInteger",
        "ResultCount",
        "Diagnostics",
        "Current number of resolved subelements",
        0,
    )
    _add_property(
        obj,
        "App::PropertyString",
        "ResolutionStatus",
        "Diagnostics",
        "Current resolution state",
        "Not evaluated",
    )
    _add_property(
        obj,
        "App::PropertyString",
        "DesignIntent",
        "Diagnostics",
        "Human-readable semantic query description",
        "",
    )
    _add_property(
        obj,
        "App::PropertyFloat",
        "ResolutionTimeMs",
        "Diagnostics",
        "Duration of the most recent evaluation in milliseconds",
        0.0,
    )
    _add_property(
        obj,
        "App::PropertyBool",
        "TopologyRenumbered",
        "Diagnostics",
        "True if current resolved subelement names differ from original captured selection",
        False,
    )
    _add_property(
        obj,
        "App::PropertyString",
        "RenumberingAudit",
        "Diagnostics",
        "Detailed explanation of topology renumbering accommodation",
        "Not evaluated",
    )
    _add_property(
        obj,
        "App::PropertyEnumeration",
        "HealthStatus",
        "Diagnostics",
        "Overall health state of this robust selector",
    )
    if getattr(obj, "HealthStatus", None) in (None, ""):
        obj.HealthStatus = ["OK", "Warning", "Error", "Disabled"]
        obj.HealthStatus = "OK"

    ensure_binding_properties(obj)
    # 0.2 had this field; keep old documents loadable but make it explicit that
    # it is no longer an automatic-management switch.
    if "AutoManaged" in set(getattr(obj, "PropertiesList", []) or []):
        obj.AutoManaged = False
        _set_editor_mode(obj, "AutoManaged", 2)

    _set_editor_mode(obj, "CapturedSelection", 1)
    _set_editor_mode(obj, "Resolved", 1)
    _set_editor_mode(obj, "ResultCount", 1)
    _set_editor_mode(obj, "ResolutionStatus", 1)
    _set_editor_mode(obj, "DesignIntent", 1)
    _set_editor_mode(obj, "ResolutionTimeMs", 1)
    _set_editor_mode(obj, "TopologyRenumbered", 1)
    _set_editor_mode(obj, "RenumberingAudit", 1)
    _set_editor_mode(obj, BINDING_STATUS_PROPERTY, 1)
    return obj


class SelectorObjectProxy:
    """FeaturePython proxy evaluated by the normal FreeCAD recompute engine."""

    def __init__(self, obj: Any):
        self.Type = "FeatureSelector"
        obj.Proxy = self
        ensure_selector_properties(obj)

    def execute(self, obj: Any) -> None:
        import time
        t0 = time.perf_counter()
        ensure_selector_properties(obj)
        try:
            selector = Selector.from_json(obj.Query)
            if not obj.Enabled or obj.BaseObject is None:
                obj.Resolved = "[]"
                obj.ResultCount = 0
                obj.ResolutionStatus = "Disabled" if not obj.Enabled else "No source object"
                obj.HealthStatus = "Disabled" if not obj.Enabled else "Error"
                obj.DesignIntent = selector.describe()
                if not obj.Enabled:
                    obj.BindingStatus = "Binding refresh disabled"
                else:
                    obj.BindingStatus = "Binding refresh blocked: no source object"
                obj.TopologyRenumbered = False
                obj.RenumberingAudit = "Evaluation skipped: " + obj.ResolutionStatus
                obj.ResolutionTimeMs = round((time.perf_counter() - t0) * 1000.0, 3)
                return

            candidates = selector.evaluate_candidates(obj.BaseObject)
            captured = list(getattr(obj, "CapturedSelection", []) or [])
            refs = []

            if selector.expected_count is not None and len(candidates) != selector.expected_count:
                obj.Resolved = json.dumps(
                    {
                        "error": "expected_count_not_satisfied",
                        "count": len(candidates),
                        "expected": selector.expected_count,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                obj.ResultCount = len(candidates)
                obj.ResolutionStatus = f"Unresolved: {len(candidates)}/{selector.expected_count}"
                obj.HealthStatus = "Warning"
                obj.TopologyRenumbered = False
                obj.RenumberingAudit = (
                    f"Ambiguity detected: query matched {len(candidates)} candidates, "
                    f"but expected count was {selector.expected_count}. Target properties left untouched."
                )
            else:
                refs = [candidate.ref for candidate in candidates]
                obj.Resolved = json.dumps(selection_pairs(refs), separators=(",", ":"))
                obj.ResultCount = len(refs)
                obj.ResolutionStatus = "Resolved" if refs else "Resolved to empty selection"
                obj.HealthStatus = "OK" if refs else "Warning"

                # Check if topology renumbering occurred
                resolved_subnames = [ref.subname for ref in refs]
                if captured and resolved_subnames:
                    if set(captured) != set(resolved_subnames):
                        obj.TopologyRenumbered = True
                        obj.RenumberingAudit = (
                            f"Topological renumbering detected! Original: {captured} -> "
                            f"Current: {resolved_subnames}. Semantic selector successfully mapped intended geometry."
                        )
                    else:
                        obj.TopologyRenumbered = False
                        obj.RenumberingAudit = f"Topology matches captured subelements: {captured}"
                else:
                    obj.TopologyRenumbered = False
                    obj.RenumberingAudit = "Initial evaluation."

            obj.DesignIntent = selector.describe()
            from fs_bindings import auto_update_consumers, build_proxy_shape

            # Publish shape BEFORE updating consumers: their synchronous
            # re-attach on support change must already see the new geometry,
            # otherwise the attachment engine reports a momentary
            # "subshape not found" that is pure noise.  Unresolved selectors
            # keep their last good shape and leave consumers untouched.
            #
            # The location split is explicit on purpose: assigning a located
            # subshape to .Shape implicitly moves its location into the
            # feature Placement outside recompute, but silently drops it
            # inside execute().  Storing unlocated geometry plus the location
            # as Placement behaves identically in every context.
            if hasattr(obj, "Shape") and obj.BaseObject is not None and hasattr(obj.BaseObject, "Shape"):
                import FreeCAD as _App
                proxy_shape, _names = build_proxy_shape(obj.BaseObject, refs)
                if proxy_shape is not None:
                    location = proxy_shape.Placement
                    unlocated = proxy_shape.copy()
                    unlocated.Placement = _App.Placement()
                    obj.Shape = unlocated
                    obj.Placement = location
            auto_update_consumers(obj, refs)
            obj.ResolutionTimeMs = round((time.perf_counter() - t0) * 1000.0, 3)
        except Exception as exc:
            obj.Resolved = json.dumps({"error": str(exc)}, separators=(",", ":"), sort_keys=True)
            obj.ResultCount = 0
            obj.ResolutionStatus = f"Error: {exc}"
            obj.HealthStatus = "Error"
            obj.TopologyRenumbered = False
            obj.RenumberingAudit = f"Evaluation error: {exc}"
            obj.ResolutionTimeMs = round((time.perf_counter() - t0) * 1000.0, 3)
            obj.DesignIntent = str(obj.Query)
            obj.BindingStatus = f"Binding refresh blocked: {exc}"

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class ViewProviderSelector:
    """View provider giving FeatureSelector a distinct icon, tree menu, and double-click editor."""

    def __init__(self, vobj: Any):
        vobj.Proxy = self

    def getIcon(self) -> str:
        try:
            import os
            from fs_commands import _ICONS_DIR
            icon_path = os.path.join(_ICONS_DIR, "FeatureSelector.svg")
            if os.path.exists(icon_path):
                return icon_path
        except Exception:
            pass
        return "FeatureSelector.svg"

    def doubleClicked(self, vobj: Any) -> bool:
        """Double-clicking in the Model Tree edits the selector via the standard task flow.

        Delegates to ``Gui.ActiveDocument.setEdit`` so FreeCAD itself handles the
        standard "close/cancel the previous task" request when another operation
        (Sketch, Pad, Pocket, ...) is currently being edited, exactly like native
        operations behave.
        """
        try:
            import FreeCADGui as Gui
            gui_doc = getattr(Gui, "ActiveDocument", None)
            if gui_doc is not None:
                try:
                    gui_doc.setEdit(getattr(vobj, "Object", None), 0)
                    return True
                except Exception:
                    pass
            # Fallback when no GUI document is available (headless tests).
            from fs_gui import FeatureSelectorPanel
            panel = FeatureSelectorPanel.instance()
            panel.edit_selector_object(vobj.Object)
            return True
        except Exception:
            return False

    def setEdit(self, vobj: Any, mode: int = 0) -> bool:
        """Enter edit mode through FreeCAD's standard task-view machinery."""
        try:
            from fs_gui import FeatureSelectorPanel
            panel = FeatureSelectorPanel.instance()
            panel.edit_selector_object(getattr(vobj, "Object", vobj))
            return True
        except Exception:
            return False

    def unsetEdit(self, vobj: Any, mode: int = 0) -> None:
        """Leave edit mode; cleanup only (FreeCAD core already closes the dialog)."""
        try:
            from fs_gui import FeatureSelectorPanel
            panel = FeatureSelectorPanel.instance()
            panel.notify_edit_closed()
        except Exception:
            pass

    def setupContextMenu(self, vobj: Any, menu: Any) -> None:
        """Add context menu actions to the tree view item."""
        try:
            from PySide import QtGui
            action_edit = QtGui.QAction("Edit Robust Selector…", menu)
            action_edit.triggered.connect(lambda: self.doubleClicked(vobj))
            menu.addAction(action_edit)

            action_preview = QtGui.QAction("Highlight in 3D View", menu)
            action_preview.triggered.connect(lambda: self._preview(vobj))
            menu.addAction(action_preview)

            action_recompute = QtGui.QAction("Recompute Selector", menu)
            action_recompute.triggered.connect(lambda: self._recompute(vobj))
            menu.addAction(action_recompute)

            action_unbind = QtGui.QAction("Unbind from All Consumers", menu)
            action_unbind.triggered.connect(lambda: self._unbind_all(vobj))
            menu.addAction(action_unbind)

            action_copy_py = QtGui.QAction("Copy Python Code Snippet", menu)
            action_copy_py.triggered.connect(lambda: self._copy_py(vobj))
            menu.addAction(action_copy_py)
        except Exception:
            pass

    def _preview(self, vobj: Any) -> None:
        try:
            from fs_freecad import add_selection
            from fs_selector import Selector
            obj = vobj.Object
            selector = Selector.from_json(obj.Query)
            if obj.BaseObject:
                add_selection(selector.evaluate(obj.BaseObject), clear=True)
        except Exception:
            pass

    def _copy_py(self, vobj: Any) -> None:
        try:
            from PySide import QtWidgets
            from fs_selector import Selector, selector_to_python
            obj = vobj.Object
            selector = Selector.from_json(obj.Query)
            source_name = getattr(getattr(obj, "BaseObject", None), "Name", "obj")
            code = selector_to_python(selector, source_name)
            cb = QtWidgets.QApplication.clipboard()
            if cb is not None:
                cb.setText(code)
            if hasattr(App, "Console"):
                App.Console.PrintMessage(f"FeatureSelector: Copied Python snippet for {obj.Label} to clipboard.\n")
        except Exception:
            pass

    def _recompute(self, vobj: Any) -> None:
        try:
            obj = vobj.Object
            if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "execute"):
                obj.Proxy.execute(obj)
            doc = getattr(obj, "Document", None)
            if doc:
                doc.recompute()
        except Exception:
            pass

    def _unbind_all(self, vobj: Any) -> None:
        try:
            from fs_bindings import unbind_selector
            obj = vobj.Object
            unbind_selector(obj)
            doc = getattr(obj, "Document", None)
            if doc:
                doc.recompute()
        except Exception:
            pass

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None





def create_selector_object(
    doc: Any,
    source_obj: Any,
    selector: Selector,
    label: str | None = None,
    captured_selection: Iterable[str] = (),
) -> Any:
    """Create one explicit persistent selector object.

    The selector lives WITH its source: when the source is inside a PartDesign
    Body, the selector is a ``PartDesign::FeaturePython`` inserted into that
    Body immediately after the source, keeping everything self-contained in the
    PartDesign history tree.  It chains on the source solid (``BaseFeature``)
    when the source is a PartDesign feature, and the owning Body's ``Tip`` is
    preserved whenever it sits downstream of the insertion point, so existing
    features (Pockets, Fillets, ...) are never visually lost; when created at
    the end of the history the selector becomes the new end like any native
    feature, so later operations append after it in the right order.
    When the source is outside any Body, the selector is a
    ``Part::FeaturePython`` in the "Robust Selections" group.
    The caller controls the surrounding document transaction and recompute.
    """
    name = "FeatureSelector"
    suffix = 1
    while doc.getObject(name):
        suffix += 1
        name = f"FeatureSelector{suffix}"

    if document_name(source_obj) != getattr(doc, "Name", ""):
        raise ValueError(
            f"Cannot create robust selector in document '{getattr(doc, 'Name', '')}': "
            f"source '{object_name(source_obj)}' belongs to document "
            f"'{document_name(source_obj)}'. Insert an App::Link to the external part "
            f"in this document and select through the link instead."
        )

    body = _find_parent_body(source_obj)
    if body is not None:
        order_before = list(body.Group)
        tip_before = body.Tip
        obj = doc.addObject("PartDesign::FeaturePython", name)
        body.addObject(obj)
        # Chain on the source solid so the Body treats the selector as a
        # regular history step; otherwise stay a side-branch with no solid.
        if source_obj.isDerivedFrom("PartDesign::Feature") and source_obj in order_before:
            obj.BaseFeature = source_obj
        elif obj.BaseFeature is not None:
            obj.BaseFeature = None
        # Order right after the source so Pad -> selector -> sketch/pocket
        # executes in dependency order.
        grp = list(body.Group)
        grp.remove(obj)
        grp.insert(grp.index(source_obj) + 1, obj)
        body.Group = grp
        # Preserve the Tip only when it sits downstream of the insertion point
        # (robustifying an existing Pocket must not "lose" it). When the
        # selector is created at the end (Tip == source), it becomes the new
        # end like any native feature, so later features append after it.
        if (
            tip_before is not None
            and tip_before in order_before
            and source_obj in order_before
            and order_before.index(tip_before) > order_before.index(source_obj)
        ):
            body.Tip = tip_before
    else:
        obj = doc.addObject("Part::FeaturePython", name)
        group = doc.getObject(SELECTOR_GROUP_NAME)
        if group is None:
            group = doc.addObject("App::DocumentObjectGroup", SELECTOR_GROUP_NAME)
            group.Label = SELECTOR_GROUP_LABEL
        group.addObject(obj)

    obj.Label = label or f"Robust: {selector.describe()}"
    ensure_selector_properties(obj)
    obj.FeatureKind = selector.kind
    obj.Query = selector.to_json()
    obj.CapturedSelection = [str(name) for name in captured_selection if str(name)]
    obj.ExpectedCount = int(selector.expected_count if selector.expected_count is not None else -1)
    obj.Enabled = True
    obj.BaseObject = source_obj
    SelectorObjectProxy(obj)
    if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
        try:
            ViewProviderSelector(obj.ViewObject)
        except (AttributeError, RuntimeError):
            pass
    return obj
