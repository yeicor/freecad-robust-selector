"""Streamlined, native-style FreeCAD task panel for robust feature selection.

Uses CadQuery selector expression language for concise, powerful geometric queries
with subelement descending, cursor-based autocompletion, and native FreeCAD OK/Cancel tasks.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Optional, Sequence

import FreeCAD as App
import FreeCADGui as Gui
try:
    from PySide import QtCore, QtWidgets, QtGui
except ImportError:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets, QtGui

from fs_bindings import (
    find_matching_property,
    object_name,
)
from fs_document import create_selector_object, ensure_selector_properties
from fs_expression import (
    autocomplete_at_cursor,
    evaluate_expression,
    evaluate_expression_items,
    parse_expression,
    steps_to_expression,
)
from fs_freecad import add_selection, shape_type_from_subname
from fs_planner import Plan, plan_selectors
from fs_selector import FeatureRef, Selector, Step, candidates, candidate_for


PANEL_OBJECT_NAME = "FeatureSelectorPanel"


# ---------------------------------------------------------------------------
#  Mouse wheel filter - prevents accidental edits when scrolling the panel
# ---------------------------------------------------------------------------

class NoWheelFilter(QtCore.QObject):
    """Event filter that ignores wheel events so scrolling scrolls the panel."""

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def _no_popup() -> bool:
    return os.environ.get("FREECAD_NO_POPUP", "").strip().lower() in {"1", "true", "yes"}


def _notify_user(parent: Optional[QtWidgets.QWidget], title: str, text: str, level: str = "info"):
    if _no_popup():
        if hasattr(App, "Console"):
            App.Console.PrintWarning(f"[{title}] {text}\n")
        return
    fn = {
        "warning": QtWidgets.QMessageBox.warning,
        "critical": QtWidgets.QMessageBox.critical,
        "info": QtWidgets.QMessageBox.information,
    }.get(level, QtWidgets.QMessageBox.information)
    try:
        fn(parent, title, text)
    except Exception:
        pass


def _selection_snapshot() -> tuple[list[tuple[Any, list[str], str]], list[Any]]:
    """Return source subelement groups and object-only selections."""
    sources: list[tuple[Any, list[str], str]] = []
    object_only: list[Any] = []
    if Gui is None or not hasattr(Gui, "Selection"):
        return sources, object_only
    for selection in Gui.Selection.getSelectionEx():
        obj = selection.Object
        names = [str(name) for name in selection.SubElementNames]
        if names:
            by_kind: dict[str, list[str]] = {}
            for name in names:
                kind = shape_type_from_subname(name) or "Shape"
                by_kind.setdefault(kind, []).append(name)
            for kind, grouped in by_kind.items():
                sources.append((obj, grouped, kind))
        else:
            object_only.append(obj)
    return sources, object_only


def _distinct(seq: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in seq:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


PRESETS: list[tuple[str, str]] = [
    ("⚡ Presets / Cheat Sheet…", ""),
    ("Top Face (>Z)", ">Z"),
    ("Bottom Face (<Z)", "<Z"),
    ("Right Face (>X)", ">X"),
    ("Left Face (<X)", "<X"),
    ("Back Face (>Y)", ">Y"),
    ("Front Face (<Y)", "<Y"),
    ("Vertical (|Z)", "|Z"),
    ("Perpendicular to Z (#Z)", "#Z"),
    ("Normal pointing +Z (+Z)", "+Z"),
    ("Normal pointing -Z (-Z)", "-Z"),
    ("Planar Faces (%Plane)", "%Plane"),
    ("Cylindrical Faces (%Cylinder)", "%Cylinder"),
    ("Linear Edges (%Line)", "%Line"),
    ("Circular Edges (%Circle)", "%Circle"),
    ("Edges of Top Face (faces('>Z').edges())", 'faces(">Z").edges()'),
    ("Edges of Top Face Left (faces('>Z').edges('<X'))", 'faces(">Z").edges("<X")'),
    ("Vertices of Top Face (faces('>Z').vertices())", 'faces(">Z").vertices()'),
    ("Intersection (and)", " and "),
    ("Union (or)", " or "),
    ("Negation (not)", "not "),
    ("Set Difference (exc)", " exc "),
]


# Lightweight compatibility shim for StepRow
class StepRow(QtWidgets.QWidget):
    changed = QtCore.Signal()
    move_requested = QtCore.Signal(int)
    remove_requested = QtCore.Signal()
    insert_requested = QtCore.Signal()

    def __init__(self, step: Any = None, index: int = 0, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.step = step or Step("extreme", {"metric": "z", "direction": "max"})
        self.index = index
        self.force = QtWidgets.QCheckBox()
        self.remove_btn = QtWidgets.QPushButton()
        self.remove_btn.clicked.connect(self.remove_requested)

    def current_step(self) -> Step:
        return self.step


# ---------------------------------------------------------------------------
#  FeatureSelectorPanel – streamlined FreeCAD TaskPanel
# ---------------------------------------------------------------------------

class FeatureSelectorPanel:
    _instance: Optional["FeatureSelectorPanel"] = None

    @classmethod
    def instance(cls) -> "FeatureSelectorPanel":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.form: Optional[QtWidgets.QWidget] = None
        self.title: str = "Feature Selector"
        self.source_obj: Any = None
        self.kind: Optional[str] = None
        self.target_subnames: list[str] = []
        self.captured_selection: list[str] = []
        self.plans: list[Plan] = []
        self.steps: list[Step] = []
        self.selector_obj: Any = None
        self.last_resolved: list[Any] = []
        self._updating: bool = False
        self._dialog_open: bool = False
        self._plan_generation: int = 0
        self._plan_task: Any = None
        self._routes_complete: bool = True
        self._build_ui()

    def _next_plan_generation(self) -> int:
        self._plan_generation += 1
        return self._plan_generation

    def _planning_succeeded(self, generation: int, doc: Any, callback: Any, plans: list[Any]) -> None:
        if generation != self._plan_generation:
            return
        if callback:
            callback(plans)

    def getTitle(self) -> str:
        return "Feature Selector"

    def getStandardButtons(self) -> int:
        """Native FreeCAD task buttons: OK and Cancel displayed above the panel."""
        btn_ok = getattr(QtWidgets.QDialogButtonBox.StandardButton, "Ok", getattr(QtWidgets.QDialogButtonBox, "Ok", 0x00000400))
        btn_cancel = getattr(QtWidgets.QDialogButtonBox.StandardButton, "Cancel", getattr(QtWidgets.QDialogButtonBox, "Cancel", 0x00400000))
        val_ok = getattr(btn_ok, "value", btn_ok)
        val_cancel = getattr(btn_cancel, "value", btn_cancel)
        return int(val_ok | val_cancel)

    # ------------------------------------------------------------------
    #  UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Title header ──
        self.title_label = QtWidgets.QLabel("<b>Feature Selector</b>")
        self.title_label.setStyleSheet("font-size: 13px; padding-bottom: 2px;")
        layout.addWidget(self.title_label)

        # ── Row 1: Target feature(s) and [Set as Target] & [Change] buttons ──
        target_row = QtWidgets.QHBoxLayout()
        target_row.setSpacing(4)
        self.target_label = QtWidgets.QLabel("Target: — (select in 3D view)")
        self.target_label.setWordWrap(True)
        self.target_label.setStyleSheet("font-weight: bold; padding: 2px;")
        target_row.addWidget(self.target_label, 1)

        self.set_target_btn = QtWidgets.QPushButton("Set as Target")
        self.set_target_btn.setToolTip("Set the current selector result as the new target (exact match).")
        self.set_target_btn.clicked.connect(self.set_target_from_selector)
        self.use_as_target_btn = self.set_target_btn
        target_row.addWidget(self.set_target_btn)

        self.change_btn = QtWidgets.QPushButton("Change")
        self.change_btn.setToolTip("Capture the current FreeCAD 3D selection as the target.")
        self.change_btn.setMaximumWidth(70)
        self.change_btn.clicked.connect(self.change_target)
        target_row.addWidget(self.change_btn)
        layout.addLayout(target_row)

        # ── Row 2: Merged Compact Status & Live preview checkbox ──
        status_row = QtWidgets.QHBoxLayout()
        status_row.setSpacing(4)
        self.current_result = QtWidgets.QLabel("Result: —")
        self.current_result.setWordWrap(True)
        self.current_result.setStyleSheet("color: palette(mid);")
        status_row.addWidget(self.current_result, 1)

        self.live_preview_cb = QtWidgets.QCheckBox("Preview")
        self.live_preview_cb.setChecked(True)
        self.live_preview_cb.setToolTip("Live 3D highlight of matching subelements.")
        self.live_preview_cb.toggled.connect(self._toggle_live_preview)
        status_row.addWidget(self.live_preview_cb)
        layout.addLayout(status_row)

        # Compatibility aliases for legacy tests
        self.status = self.current_result
        self.source_label = self.target_label
        self.diagnostics_label = self.current_result

        # ── Row 3: CadQuery Selector Expression Editor ──
        self.expr_edit = QtWidgets.QPlainTextEdit()
        self.expr_edit.setPlaceholderText('e.g. >Z, |Z and >Y, faces(">Z").edges("<X")')
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.expr_edit.setFont(font)
        self.expr_edit.setMinimumHeight(60)
        self.expr_edit.setMaximumHeight(90)
        self.expr_edit.textChanged.connect(self._on_expr_changed)
        layout.addWidget(self.expr_edit)

        # ── Row 4: Action / Helper row: Autocomplete, Presets, Help ──
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(4)

        self.autocomplete_btn = QtWidgets.QPushButton("⚡ Autocomplete")
        self.autocomplete_btn.setToolTip("Find selector matching selection and insert at cursor position.")
        self.autocomplete_btn.clicked.connect(self.autocomplete_in_cursor)
        action_row.addWidget(self.autocomplete_btn)

        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.installEventFilter(NoWheelFilter(self.preset_combo))
        for label, _data in PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.preset_combo.setToolTip("Insert selector snippet at cursor.")
        action_row.addWidget(self.preset_combo, 1)

        self.help_btn = QtWidgets.QToolButton()
        self.help_btn.setText("?")
        self.help_btn.setToolTip("CadQuery selector guide and syntax help")
        self.help_btn.clicked.connect(self.show_quick_guide)
        action_row.addWidget(self.help_btn)
        layout.addLayout(action_row)

        layout.addStretch(1)

        scroll_outer = QtWidgets.QScrollArea()
        scroll_outer.setWidgetResizable(True)
        scroll_outer.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll_outer.setWidget(root)
        scroll_outer.setWindowTitle("Feature Selector")
        self.form = scroll_outer

    # ---------- lifecycle / dialog ----------

    @staticmethod
    def _task_dialog_open() -> bool:
        try:
            return bool(Gui.Control.activeDialog())
        except Exception:
            return False

    def _is_own_dialog_active(self) -> bool:
        return self._task_dialog_open() and bool(getattr(self, "_dialog_open", False))

    def _confirm_close_active_task(self) -> bool:
        if not self._task_dialog_open() or self._is_own_dialog_active():
            return True
        if _no_popup():
            return True
        try:
            parent = Gui.getMainWindow()
        except Exception:
            parent = None
        try:
            answer = QtWidgets.QMessageBox.question(
                parent,
                "Close current task?",
                "Another operation is currently being edited.\n"
                "Close it and open the Feature Selector instead?",
                QtWidgets.QMessageBox.StandardButton.Ok
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            return answer == QtWidgets.QMessageBox.StandardButton.Ok
        except Exception:
            return True

    def show_panel(self) -> bool:
        if Gui:
            if self._is_own_dialog_active():
                if self.has_active_selector():
                    self._update_merged_status()
                return True
            if self._task_dialog_open():
                if not self._confirm_close_active_task():
                    return False
                try:
                    Gui.Control.closeDialog()
                except Exception:
                    pass
            self._build_ui()
            if self.has_active_selector():
                self._update_merged_status()
            try:
                Gui.Control.showDialog(self)
            except RuntimeError:
                self._dialog_open = False
                return False
            self._dialog_open = True
            return True
        return False

    def edit_selector_object(self, selector_obj: Any) -> bool:
        try:
            shown = self.show_panel()
        except RuntimeError:
            return False
        if shown is False:
            return False
        self.load_selector_object(selector_obj)
        return True

    def open_editor_for_object(self, selector_obj: Any) -> bool:
        if Gui:
            gui_doc = getattr(Gui, "ActiveDocument", None)
            if gui_doc is not None and hasattr(gui_doc, "setEdit"):
                if gui_doc.setEdit(selector_obj, 0):
                    return True
        return self.edit_selector_object(selector_obj)

    def notify_edit_closed(self) -> None:
        self._dialog_open = False
        if Gui is not None:
            Gui.Selection.clearSelection()

    def close_panel(self):
        owns_dialog = False
        if Gui:
            owns_dialog = self._is_own_dialog_active()
        self._dialog_open = False
        if Gui:
            gui_doc = getattr(Gui, "ActiveDocument", None)
            if gui_doc is not None and hasattr(gui_doc, "getInEdit"):
                in_edit = gui_doc.getInEdit()
                if in_edit is not None and self._edit_owns(in_edit):
                    gui_doc.resetEdit()
                    return
            if owns_dialog:
                Gui.Control.closeDialog()

    def _edit_owns(self, in_edit: Any) -> bool:
        if self.selector_obj is None or in_edit is None:
            return False
        edited_obj = getattr(in_edit, "Object", None)
        if edited_obj is self.selector_obj:
            return True
        if edited_obj is not None and getattr(edited_obj, "Name", None) == getattr(self.selector_obj, "Name", None):
            return True
        return in_edit is getattr(self.selector_obj, "ViewObject", None)

    def accept(self) -> bool:
        """Triggered by standard OK button."""
        self.save_selector()
        self.close_panel()
        return True

    def reject(self) -> bool:
        """Triggered by standard Cancel button."""
        try:
            if Gui is not None:
                Gui.Selection.clearSelection()
        except Exception:
            pass
        self.close_panel()
        return True

    def has_active_selector(self) -> bool:
        if self.source_obj is None or self.kind is None or not bool(self.target_subnames):
            return False
        try:
            _ = self.source_obj.Name
            return True
        except ReferenceError:
            self.source_obj = None
            return False

    def current_selector(self) -> Optional[Selector]:
        if self.source_obj is None or self.kind is None:
            return None
        expr = self.expr_edit.toPlainText().strip() if hasattr(self, "expr_edit") and self.expr_edit else ""
        return Selector(
            kind=self.kind,
            steps=tuple(self.steps),
            expected_count=len(self.target_subnames),
            name="interactive",
            expression=expr,
        )

    def _refuse_foreign_source(self, source_obj: Any) -> bool:
        active = getattr(App, "ActiveDocument", None)
        if active is None or source_obj is None:
            return False
        source_doc = getattr(source_obj, "Document", None)
        if source_doc is not None and getattr(source_doc, "Name", None) == active.Name:
            return False
        _notify_user(
            Gui.getMainWindow() if Gui else None,
            "Feature Selector",
            "That geometry belongs to another document. Insert an App::Link to the "
            "external part in this document and select through the link instead.",
            "warning",
        )
        self.source_obj = None
        self.kind = None
        self.target_subnames = []
        self.current_result.setText("Select geometry in the active document.")
        return True

    # ---------- Selection capture & change ----------

    def change_target(self):
        """Update target feature(s) from active 3D selection."""
        sources, object_only = _selection_snapshot()
        if not sources:
            if len(object_only) == 1:
                obj = object_only[0]
                if hasattr(obj, "Query") and hasattr(obj, "BaseObject") and obj.BaseObject is not None:
                    self.load_selector_object(obj)
                    return
                # Subelement consumer or whole shape
                from fs_bindings import inspect_feature_references
                refs_info = inspect_feature_references(obj)
                if refs_info:
                    sub_refs = [r for r in refs_info if r.get("subnames") and r.get("subnames") != [""] and r.get("kind") != "Shape"]
                    info = sub_refs[0] if sub_refs else refs_info[0]
                    if self._refuse_foreign_source(info["source"]):
                        return
                    self._reset_target_state(info["source"], info["kind"], info["subnames"])
                    return
                shape = getattr(obj, "Shape", None)
                if shape is not None and not shape.isNull():
                    if self._refuse_foreign_source(obj):
                        return
                    self._reset_target_state(obj, "Shape", [""])
                    return

            _notify_user(
                Gui.getMainWindow() if Gui else None,
                "Feature Selector",
                "Select subelements (faces, edges, etc.) in the 3D view, then click Change.",
                "info",
            )
            return

        source_objects = {id(item[0]) for item in sources}
        if len(source_objects) != 1:
            _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", "Select subelements from one source object at a time.", "warning")
            return
        kinds = {item[2] for item in sources}
        if len(kinds) != 1:
            _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", "Selected subelements must be the same kind (all Faces or all Edges).", "warning")
            return

        source_obj = sources[0][0]
        if self._refuse_foreign_source(source_obj):
            return
        kind = next(iter(kinds))
        target_subnames = _distinct([name for _obj, names, _kind in sources for name in names])
        self._reset_target_state(source_obj, kind, target_subnames)

    def _reset_target_state(self, source: Any, kind: str, subnames: list[str]):
        self.source_obj = source
        self.kind = kind
        self.target_subnames = list(subnames)
        self.captured_selection = list(subnames)
        self.selector_obj = None
        if hasattr(self, "expr_edit") and self.expr_edit:
            self._updating = True
            try:
                self.expr_edit.clear()
            finally:
                self._updating = False
        self._update_merged_status()

    def learn(self):
        """Capture selection (alias for change_target)."""
        self.change_target()

    def load_selector_object(self, selector_obj: Any):
        try:
            selector = Selector.from_json(selector_obj.Query)
            source = selector_obj.BaseObject
            if source is None:
                raise ValueError("The selector has no source object")
            captured = list(getattr(selector_obj, "CapturedSelection", []) or [])
            if not captured:
                resolved = selector.evaluate_candidates(source)
                captured = [c.ref.subname for c in resolved]
            self.selector_obj = selector_obj
            self.source_obj = source
            self.kind = selector.kind
            self.target_subnames = captured
            self.captured_selection = captured
            self.steps = list(selector.steps)
            expr = selector.expression or selector.describe()
            self._updating = True
            try:
                self.expr_edit.setPlainText(expr)
            finally:
                self._updating = False
            self._update_merged_status()
            if hasattr(source, "ViewObject") and source.ViewObject is not None:
                source.ViewObject.Visibility = True
            refs = selector.evaluate(source)
            if refs:
                add_selection(refs, clear=True)
        except Exception as exc:
            _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", f"Could not open selector: {exc}", "warning")

    # ---------- Expression editing & Autocompletion ----------

    def _on_expr_changed(self):
        if self._updating:
            return
        self._update_merged_status()

    def autocomplete_in_cursor(self):
        """Autocomplete at cursor position to find matching selector."""
        if not self.source_obj or not self.target_subnames or not self.kind:
            return
        current_text = self.expr_edit.toPlainText()
        cursor = self.expr_edit.textCursor()
        pos = cursor.position()

        to_insert, _ = autocomplete_at_cursor(
            self.source_obj,
            self.target_subnames,
            self.kind,
            current_text,
            pos,
        )
        if to_insert:
            cursor.insertText(to_insert)
            self.expr_edit.setTextCursor(cursor)

    def _preset_chosen(self, index: int):
        if index <= 0 or index >= len(PRESETS):
            return
        _label, snippet = PRESETS[index]
        self.preset_combo.setCurrentIndex(0)
        if snippet:
            cursor = self.expr_edit.textCursor()
            cursor.insertText(snippet)
            self.expr_edit.setTextCursor(cursor)
            self.expr_edit.setFocus()

    def set_target_from_selector(self):
        """Adopt the current selector's evaluated features as the new target."""
        if not self.source_obj:
            sources, object_only = _selection_snapshot()
            if sources:
                self.source_obj = sources[0][0]
                if not self.kind:
                    self.kind = sources[0][2]
            elif object_only:
                self.source_obj = object_only[0]
            elif App and getattr(App, "ActiveDocument", None):
                active = getattr(App.ActiveDocument, "ActiveObject", None)
                if active and hasattr(active, "Shape"):
                    self.source_obj = active

        if not self.source_obj:
            _notify_user(
                Gui.getMainWindow() if Gui else None,
                "Feature Selector",
                "Please select an object first so the selector can evaluate its features.",
                "info",
            )
            return

        expr = self.expr_edit.toPlainText().strip()
        if not expr:
            _notify_user(
                Gui.getMainWindow() if Gui else None,
                "Feature Selector",
                "Enter a selector expression first (e.g. >Z, |Z, faces('>Z').edges()).",
                "info",
            )
            return

        try:
            matched_items = evaluate_expression_items(self.source_obj, expr, kind=self.kind)
        except Exception as exc:
            _notify_user(
                Gui.getMainWindow() if Gui else None,
                "Feature Selector",
                f"Cannot evaluate selector: {exc}",
                "warning",
            )
            return

        if not matched_items:
            _notify_user(
                Gui.getMainWindow() if Gui else None,
                "Feature Selector",
                "Current selector matches 0 features. Cannot use empty selection as target.",
                "warning",
            )
            return

        resolved_names = [item.subname for item in matched_items if getattr(item, "subname", "")]
        inferred_kind = matched_items[0].kind if matched_items else (self.kind or "Face")
        self.kind = inferred_kind
        self.target_subnames = list(resolved_names)
        self.captured_selection = list(resolved_names)
        self.selector_obj = None
        self._update_merged_status()

    def use_as_target(self):
        """Alias for set_target_from_selector."""
        self.set_target_from_selector()

    def _update_merged_status(self):
        if not self.source_obj:
            sources, object_only = _selection_snapshot()
            if sources:
                self.source_obj = sources[0][0]
                if not self.kind:
                    self.kind = sources[0][2]
                if not self.target_subnames:
                    self.target_subnames = _distinct([name for _obj, names, _kind in sources for name in names])
                    self.captured_selection = list(self.target_subnames)
            elif object_only:
                self.source_obj = object_only[0]
            elif App and getattr(App, "ActiveDocument", None):
                active = getattr(App.ActiveDocument, "ActiveObject", None)
                if active and hasattr(active, "Shape"):
                    self.source_obj = active

        if not self.source_obj:
            self.target_label.setText("Target: — (select in 3D view and click Change)")
            self.current_result.setText("Result: —")
            self.current_result.setStyleSheet("color: palette(mid);")
            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(False)
            return

        obj_label = getattr(self.source_obj, "Label", getattr(self.source_obj, "Name", "Object"))
        count = len(self.target_subnames) if self.target_subnames else 0
        if self.target_subnames:
            names = ", ".join(self.target_subnames[:4])
            if count > 4:
                names += f" +{count - 4} more"
            self.target_label.setText(f"Target: {obj_label} [{names}]")
        else:
            self.target_label.setText(f"Target: {obj_label} (no target set)")

        expr = self.expr_edit.toPlainText().strip()
        if not expr:
            self.last_resolved = []
            if count > 0:
                self.current_result.setText(f"Waiting for selector ({count} {self.kind or 'feature'} target)")
            else:
                self.current_result.setText("Enter selector expression or select in 3D view")
            self.current_result.setStyleSheet("color: palette(mid);")
            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(False)
            return

        t0 = time.perf_counter()
        try:
            matched_items = evaluate_expression_items(self.source_obj, expr, kind=self.kind)
            self.last_resolved = matched_items
            t_ms = (time.perf_counter() - t0) * 1000.0
            matched_names = {item.subname for item in matched_items}
            m_count = len(matched_items)
            res_kind = matched_items[0].kind if matched_items else (self.kind or "Feature")

            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(m_count > 0)

            if self.target_subnames:
                target_set = set(self.target_subnames)
                if matched_names == target_set:
                    self.current_result.setText(f"✓ {m_count} {self.kind or res_kind}(s) (Exact match) · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #44cc44;")
                    exact_sel = Selector(
                        kind=self.kind or res_kind,
                        steps=tuple(self.steps),
                        expected_count=len(self.target_subnames),
                        name="interactive",
                        expression=expr,
                    )
                    self.plans = [Plan(selector=exact_sel, score=(1, 0, 0), explanation="Exact match")]
                else:
                    self.plans = []
                    self.current_result.setText(f"⚠ {m_count} {self.kind or res_kind}(s) (not exact, {count} target) · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #ccaa44;")
            else:
                self.plans = []
                self.current_result.setText(f"Found {m_count} {res_kind}(s) · {t_ms:.1f}ms (click Set as Target)")
                self.current_result.setStyleSheet("font-weight: bold; color: #44aacc;")

            if self.live_preview_cb.isChecked():
                self._update_3d_preview([item.shape for item in matched_items])
        except Exception as exc:
            self.last_resolved = []
            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(False)
            t_ms = (time.perf_counter() - t0) * 1000.0
            self.current_result.setText(f"❌ Syntax/Evaluation: {exc} · {t_ms:.1f}ms")
            self.current_result.setStyleSheet("font-weight: bold; color: #cc4444;")

    def _toggle_live_preview(self, checked: bool):
        if checked:
            self._update_merged_status()
        else:
            try:
                if Gui is not None:
                    Gui.Selection.clearSelection()
            except Exception:
                pass

    def _update_3d_preview(self, shapes: Optional[list[Any]] = None):
        if not self.live_preview_cb.isChecked() or not self.source_obj:
            return
        try:
            expr = self.expr_edit.toPlainText().strip()
            if expr:
                refs = evaluate_expression(self.source_obj, expr, kind=self.kind)
                if refs:
                    add_selection(refs, clear=True)
        except Exception:
            pass

    def _exact_selector(self) -> Optional[Selector]:
        if not self.source_obj or not self.kind:
            return None
        expr = self.expr_edit.toPlainText().strip()
        if not expr:
            return None
        try:
            refs = evaluate_expression(self.source_obj, expr, kind=self.kind)
            resolved_names = {r.subname for r in refs}
            if resolved_names != set(self.target_subnames):
                return None
            return Selector(
                kind=self.kind,
                steps=tuple(self.steps),
                expected_count=len(self.target_subnames),
                name="interactive",
                expression=expr,
            )
        except Exception:
            return None

    @staticmethod
    def _transaction(doc: Any, label: str, fn):
        doc.openTransaction(label)
        try:
            result = fn()
            doc.recompute()
            doc.commitTransaction()
            return result
        except Exception:
            doc.abortTransaction()
            raise

    def _save_object_only(self, selector: Selector) -> Any:
        doc = App.ActiveDocument
        if doc is None:
            raise ValueError("No active document")
        if self.selector_obj is not None:
            obj = self.selector_obj
            ensure_selector_properties(obj)
            obj.FeatureKind = selector.kind
            obj.Query = selector.to_json()
            obj.CapturedSelection = list(self.captured_selection)
            obj.ExpectedCount = len(self.target_subnames)
            obj.BaseObject = self.source_obj
            obj.Label = f"Robust: {selector.describe()}"
            return obj
        return create_selector_object(
            doc,
            self.source_obj,
            selector,
            captured_selection=self.captured_selection,
        )

    def save_selector(self):
        selector = self._exact_selector()
        if selector is None:
            # Fall back to selector built from current expression even if not exact
            expr = self.expr_edit.toPlainText().strip()
            if not expr or not self.source_obj or not self.kind:
                return
            selector = Selector(
                kind=self.kind,
                steps=tuple(self.steps),
                expected_count=len(self.target_subnames),
                name="interactive",
                expression=expr,
            )
        doc = App.ActiveDocument
        if doc is None:
            return
        try:
            def operation():
                obj = self._save_object_only(selector)
                self.selector_obj = obj
                return obj
            obj = self._transaction(doc, "Create robust feature selector", operation)
            if self.source_obj is not None:
                try:
                    refs = selector.evaluate(self.source_obj)
                    if refs:
                        add_selection(refs, clear=True)
                except Exception:
                    pass
        except Exception as exc:
            _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", f"Could not save:\n{exc}", "critical")

    def preview_selection(self):
        selector = self._exact_selector()
        if selector is not None and self.source_obj is not None:
            add_selection(selector.evaluate(self.source_obj), clear=True)

    def copy_python_code(self):
        expr = self.expr_edit.toPlainText().strip()
        obj_name = getattr(self.source_obj, "Name", "obj") if self.source_obj else "obj"
        code = (
            f"# Generated by FreeCAD FeatureSelector\n"
            f"from fs_expression import evaluate_expression\n\n"
            f"refs = evaluate_expression({obj_name}, {repr(expr)}, kind={repr(self.kind or 'Face')})\n"
            f"print('Matched subelements:', [r.subname for r in refs])\n"
        )
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(code)
        if hasattr(App, "Console"):
            App.Console.PrintMessage("\n" + code + "\n")

    def show_quick_guide(self):
        if os.environ.get("FREECAD_NO_POPUP", "").strip().lower() in {"1", "true", "yes"}:
            if hasattr(App, "Console"):
                App.Console.PrintMessage("Feature Selector Guide: CadQuery selectors: >Z, <Z, |Z, #Z, +Z, -Z, %Plane, %Cylinder, faces('>Z').edges('<X').\n")
            return
        text = (
            "<h3>CadQuery Selectors Quick Guide</h3>"
            "<p>Feature Selector uses CadQuery selector expressions with component descending:</p>"
            "<ul>"
            "<li><b>Extrema:</b> <code>&gt;Z</code> (max Z), <code>&lt;Z</code> (min Z), <code>&gt;X</code>, <code>&lt;Y</code></li>"
            "<li><b>Orientation:</b> <code>|Z</code> (parallel to Z), <code>#Z</code> (orthogonal to Z)</li>"
            "<li><b>Normal:</b> <code>+Z</code> (normal in +Z), <code>-Z</code> (normal in -Z)</li>"
            "<li><b>Types:</b> <code>%Plane</code>, <code>%Cylinder</code>, <code>%Line</code>, <code>%Circle</code></li>"
            "<li><b>Logical:</b> <code>&gt;Z and %Plane</code>, <code>|Z or &gt;Y</code>, <code>not |Z</code></li>"
            "<li><b>Descending:</b> <code>faces('&gt;Z').edges('&lt;X')</code></li>"
            "<li><b>⚡ Autocomplete:</b> Click to auto-fill at cursor!</li>"
            "</ul>"
        )
        msg = QtWidgets.QMessageBox(Gui.getMainWindow() if Gui else None)
        msg.setWindowTitle("Feature Selector Guide")
        msg.setTextFormat(QtCore.Qt.TextFormat.RichText)
        msg.setText(text)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Information)
        msg.exec_()

    def close(self):
        self.close_panel()
