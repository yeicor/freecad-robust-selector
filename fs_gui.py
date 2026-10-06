"""Explicit, native-style Qt workflow for robust feature selection.

Nothing in this panel silently watches the document or rewrites native commands.
The user explicitly captures a selection, edits/accepts a semantic route, chooses
an optional target property, and commits the resulting document object/binding.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

import FreeCAD as App
import FreeCADGui as Gui
try:
    from PySide import QtCore, QtWidgets, QtGui
except ImportError:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets, QtGui

from fs_bindings import (
    assemble_audit_summary,
    collect_audit_snapshots,
    evaluate_audit_snapshots,
    find_bound_selector,
    find_matching_property,
    format_audit_report,
    object_name,
    property_mode,
    read_property_value,
    target_property_candidates
)
from fs_document import create_selector_object, ensure_selector_properties
from fs_freecad import add_selection, shape_type_from_subname
from fs_planner import Plan, complete_selector, plan_selectors
from fs_selector import AXES, GEOMETRY_TYPES, METRICS, OPERATION_KINDS, PREDICATE_NAMES, Selector, Step, candidates
from fs_tasks import submit as submit_background


PANEL_OBJECT_NAME = "FeatureSelectorPanel"


BOOL_PREDICATES = {
    "planar", "curved", "linear", "circular", "elliptical", "spherical",
    "cylindrical", "conical", "toroidal", "closed", "valid", "boundary",
    "manifold_edge", "non_manifold_edge", "has_holes", "has_single_wire", "has_multiple_wires",
    "isolated_vertex", "endpoint_vertex", "bbox_contains_origin",
}
AXIS_PREDICATES = {
    "axis_parallel", "axis_perpendicular", "axis_same_direction", "axis_opposite_direction",
    "positive", "negative", "facing_positive", "facing_negative",
}
COMPARISON_OPS = [">", ">=", "==", "<=", "<", "!="]
COUNT_METRICS = {"vertex_count", "edge_count", "wire_count", "face_count", "shell_count", "solid_count", "hole_count", "adjacent_face_count", "vertex_valence"}


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


def _float_spin(value: float = 0.0, maximum: float = 1e12) -> QtWidgets.QDoubleSpinBox:
    spin = QtWidgets.QDoubleSpinBox()
    spin.setDecimals(8)
    spin.setRange(-maximum, maximum)
    spin.setSingleStep(0.1)
    spin.setValue(float(value))
    return spin


def _int_spin(value: int = 1) -> QtWidgets.QSpinBox:
    spin = QtWidgets.QSpinBox()
    spin.setRange(0, 1_000_000)
    spin.setValue(int(value))
    return spin


def _combo(items: list[str], current: str | None = None) -> QtWidgets.QComboBox:
    combo = QtWidgets.QComboBox()
    combo.addItems(items)
    if current is not None:
        index = combo.findText(str(current))
        if index >= 0:
            combo.setCurrentIndex(index)
    return combo


# ---------------------------------------------------------------------------
#  Collapsible section – progressive disclosure widget
# ---------------------------------------------------------------------------

class _CollapsibleSection(QtWidgets.QWidget):
    """A titled section whose content can be toggled visible/hidden."""

    expanded = QtCore.Signal()

    def __init__(self, title: str, collapsed: bool = True, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._collapsed = collapsed
        self._title = title

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._toggle_btn = QtWidgets.QPushButton(self._header_text())
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 6px; font-weight: bold; "
            "border: none; border-bottom: 1px solid palette(mid); }"
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._toggle_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self.toggle)
        layout.addWidget(self._toggle_btn)

        self._container = QtWidgets.QWidget()
        self._container.setVisible(not collapsed)
        self._container_layout = QtWidgets.QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._container)

    def _header_text(self) -> str:
        arrow = "▶" if self._collapsed else "▼"
        return f"{arrow}  {self._title}"

    def setContentWidget(self, widget: QtWidgets.QWidget):
        self._container_layout.addWidget(widget)

    def setTitle(self, title: str):
        self._title = title
        self._toggle_btn.setText(self._header_text())

    def toggle(self):
        self._collapsed = not self._collapsed
        self._container.setVisible(not self._collapsed)
        self._toggle_btn.setText(self._header_text())
        if not self._collapsed:
            self.expanded.emit()

    def expand(self):
        if self._collapsed:
            self.toggle()

    def expand(self):
        if self._collapsed:
            self.toggle()

    def collapse(self):
        if not self._collapsed:
            self.toggle()

    def isCollapsed(self) -> bool:
        return self._collapsed


# ---------------------------------------------------------------------------
#  StepRow – compact editable selector step
# ---------------------------------------------------------------------------

class StepRow(QtWidgets.QWidget):
    """One editable selector step — compact with right-click context menu."""

    changed = QtCore.Signal()
    move_requested = QtCore.Signal(int)
    remove_requested = QtCore.Signal()
    insert_requested = QtCore.Signal()

    def __init__(self, step: Step, index: int, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.index = index
        self.step = step
        self._building = False
        self._controls: list[QtWidgets.QWidget] = []
        self._build()

    def _build(self):
        self._building = True
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(2, 1, 2, 1)
        root.setSpacing(3)

        # Force / pin indicator (visual-only clickable label)
        self.force_indicator = QtWidgets.QLabel("")
        self.force_indicator.setFixedWidth(16)
        self.force_indicator.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.force_indicator.setToolTip("📌 = pinned as design intent (right-click to toggle)")
        root.addWidget(self.force_indicator)

        # Step number
        number = QtWidgets.QLabel(f"{self.index + 1}.")
        number.setFixedWidth(16)
        root.addWidget(number)

        # Operation combo
        self.op_combo = _combo(["filter", "extreme", "sort_take"], self.step.op)
        if self.step.op not in {"filter", "extreme", "sort_take"}:
            self.op_combo.insertItem(0, self.step.op)
        self.op_combo.currentTextChanged.connect(self._op_changed)
        self.op_combo.setMaximumWidth(85)
        root.addWidget(self.op_combo)

        # Arguments area (expands)
        self.args_widget = QtWidgets.QWidget()
        self.args_layout = QtWidgets.QHBoxLayout(self.args_widget)
        self.args_layout.setContentsMargins(0, 0, 0, 0)
        self.args_layout.setSpacing(2)
        root.addWidget(self.args_widget, 1)

        # Result count label (compact)
        self.result_label = QtWidgets.QLabel("—")
        self.result_label.setFixedWidth(32)
        self.result_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.result_label.setToolTip("Candidates remaining after this step")
        root.addWidget(self.result_label)

        # Remove button
        self.remove_btn = QtWidgets.QToolButton()
        self.remove_btn.setText("×")
        self.remove_btn.setToolTip("Remove this step")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit())
        root.addWidget(self.remove_btn)

        # Force state control
        self.force = QtWidgets.QCheckBox()
        self.force.setChecked(bool(self.step.forced))
        self.force.setVisible(False)
        self.force.toggled.connect(self._force_changed)

        # Enable right-click context menu
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # Visual styling for forced rows
        self._update_force_indicator()
        self._rebuild_args()
        self._building = False

    def _show_context_menu(self, pos):
        """Right-click context menu with power-user controls."""
        menu = QtWidgets.QMenu(self)
        # Force toggle
        if self.force.isChecked():
            force_action = menu.addAction("Unpin (auto-replan)")
        else:
            force_action = menu.addAction("📌 Pin as design intent")
        force_action.triggered.connect(lambda: self.force.setChecked(not self.force.isChecked()))
        menu.addSeparator()
        menu.addAction("↑ Move Up").triggered.connect(lambda: self.move_requested.emit(-1))
        menu.addAction("↓ Move Down").triggered.connect(lambda: self.move_requested.emit(1))
        menu.addSeparator()
        menu.addAction("+ Insert Before").triggered.connect(lambda: self.insert_requested.emit())
        remove_action = menu.addAction("× Remove")
        remove_action.triggered.connect(lambda: self.remove_requested.emit())
        menu.exec_(self.mapToGlobal(pos))

    def _update_force_indicator(self):
        """Update the visual pin indicator for forced steps."""
        if self.force.isChecked():
            self.force_indicator.setText("📌")
            self.setStyleSheet("StepRow { background-color: rgba(255, 200, 50, 30); border-radius: 2px; }")
        else:
            self.force_indicator.setText("")
            self.setStyleSheet("")

    def _clear_args(self):
        while self.args_layout.count():
            item = self.args_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._controls = []

    def _watch(self, widget: QtWidgets.QWidget, signal: str):
        getattr(widget, signal).connect(self._args_changed)
        self._controls.append(widget)
        self.args_layout.addWidget(widget)

    def _rebuild_args(self):
        self._clear_args()
        op = self.step.op
        args = self.step.args
        if op == "filter":
            name = str(args.get("name", "planar"))
            name_combo = _combo(list(PREDICATE_NAMES), name)
            name_combo.currentTextChanged.connect(self._predicate_changed)
            self._controls.append(name_combo)
            self.args_layout.addWidget(name_combo)
            value = args.get("value")
            self._add_predicate_controls(name, value)
        elif op == "extreme":
            metric = _combo(list(METRICS), str(args.get("metric", "z")))
            direction = _combo(["max", "min"], str(args.get("direction", "max")))
            tolerance = _float_spin(float(args.get("tolerance", 1e-6)))
            for widget in (metric, direction, tolerance):
                self._watch(widget, "currentTextChanged" if isinstance(widget, QtWidgets.QComboBox) else "valueChanged")
        elif op == "sort_take":
            metric = _combo(list(METRICS), str(args.get("metric", "z")))
            direction = _combo(["max", "min"], str(args.get("direction", "max")))
            count = _int_spin(int(args.get("count", 1)))
            for widget in (metric, direction, count):
                self._watch(widget, "currentTextChanged" if isinstance(widget, QtWidgets.QComboBox) else "valueChanged")
        else:
            count = _int_spin(int(args.get("count", 1)))
            self._watch(count, "valueChanged")

    def _add_predicate_controls(self, name: str, value: Any):
        if name == "geom_type":
            self._watch(_combo(list(GEOMETRY_TYPES), str(value or "PLANE")), "currentIndexChanged")
        elif name in BOOL_PREDICATES:
            current = "True" if bool(value) else "False"
            self._watch(_combo(["True", "False"], current), "currentIndexChanged")
        elif name in AXIS_PREDICATES:
            axis = value[0] if isinstance(value, (list, tuple)) else value
            self._watch(_combo(list(AXES), str(axis or "Z")), "currentIndexChanged")
        elif name == "angle_to_axis":
            payload = value if isinstance(value, dict) else {}
            self._watch(_combo(list(AXES), str(payload.get("axis", "Z"))), "currentIndexChanged")
            self._watch(_float_spin(float(payload.get("value", 0.0))), "valueChanged")
            self._watch(_float_spin(float(payload.get("tolerance", 1e-5))), "valueChanged")
        elif name in {"metric_equal", "metric_compare", "count_compare"}:
            payload = value if isinstance(value, dict) else {}
            metrics = list(COUNT_METRICS) if name == "count_compare" else list(METRICS)
            self._watch(_combo(metrics, str(payload.get("metric", "z"))), "currentIndexChanged")
            if name != "metric_equal":
                self._watch(_combo(COMPARISON_OPS, str(payload.get("operator", "=="))), "currentIndexChanged")
            self._watch(_float_spin(float(payload.get("value", 0.0))), "valueChanged")
            self._watch(_float_spin(float(payload.get("tolerance", 1e-6))), "valueChanged")
        elif name == "convexity":
            self._watch(_combo(["concave", "convex", "smooth", "open", "non_manifold"], str(value or "concave")), "currentIndexChanged")
        elif name == "metric_range":
            payload = value if isinstance(value, dict) else {}
            self._watch(_combo(list(METRICS), str(payload.get("metric", "radius"))), "currentIndexChanged")
            self._watch(_float_spin(float(payload.get("min", 0.0))), "valueChanged")
            self._watch(_float_spin(float(payload.get("max", 10.0))), "valueChanged")
            self._watch(_float_spin(float(payload.get("tolerance", 1e-6))), "valueChanged")
        elif name == "bbox_touch":
            payload = value if isinstance(value, dict) else {}
            self._watch(_combo(list(AXES), str(payload.get("axis", "Z"))), "currentIndexChanged")
            self._watch(_combo(["min", "max"], str(payload.get("side", "max"))), "currentIndexChanged")
            self._watch(_float_spin(float(payload.get("tolerance", 1e-6))), "valueChanged")
        elif name == "center_on_axis":
            payload = value if isinstance(value, dict) else {}
            self._watch(_combo(list(AXES), str(payload.get("axis", "Z"))), "currentIndexChanged")
            self._watch(_float_spin(float(payload.get("tolerance", 1e-6))), "valueChanged")
        elif name == "coplanar_with":
            payload = value if isinstance(value, dict) else {}
            for k in ("plane_normal_x", "plane_normal_y", "plane_normal_z",
                       "plane_point_x", "plane_point_y", "plane_point_z"):
                self._watch(_float_spin(float(payload.get(k, 0.0))), "valueChanged")
        elif name == "coaxial_with":
            payload = value if isinstance(value, dict) else {}
            for k in ("axis_direction_x", "axis_direction_y", "axis_direction_z",
                       "axis_point_x", "axis_point_y", "axis_point_z"):
                self._watch(_float_spin(float(payload.get(k, 0.0))), "valueChanged")

    def _read_value(self) -> Any:
        name = self._controls[0].currentText() if self._controls and isinstance(self._controls[0], QtWidgets.QComboBox) else "planar"
        controls = self._controls[1:]
        if name == "geom_type":
            return controls[0].currentText()
        if name in BOOL_PREDICATES:
            return controls[0].currentText() == "True"
        if name in AXIS_PREDICATES:
            return controls[0].currentText()
        if name == "convexity":
            return controls[0].currentText()
        if name == "metric_range":
            return {
                "metric": controls[0].currentText(),
                "min": controls[1].value(),
                "max": controls[2].value(),
                "tolerance": controls[3].value(),
            }
        if name in {"bbox_touch", "center_on_axis"}:
            payload = {"axis": controls[0].currentText()}
            if name == "bbox_touch":
                payload["side"] = controls[1].currentText()
                payload["tolerance"] = controls[2].value()
            else:
                payload["tolerance"] = controls[1].value()
            return payload
        if name == "angle_to_axis":
            return {"axis": controls[0].currentText(), "value": controls[1].value(), "tolerance": controls[2].value()}
        if name in {"metric_equal", "metric_compare", "count_compare"}:
            result = {"metric": controls[0].currentText()}
            offset = 1
            if name != "metric_equal":
                result["operator"] = controls[1].currentText()
                offset = 2
            result["value"] = controls[offset].value()
            result["tolerance"] = controls[offset + 1].value()
            return result
        if name == "coplanar_with":
            return {
                "plane_normal_x": controls[0].value(), "plane_normal_y": controls[1].value(), "plane_normal_z": controls[2].value(),
                "plane_point_x": controls[3].value(), "plane_point_y": controls[4].value(), "plane_point_z": controls[5].value(),
            }
        if name == "coaxial_with":
            return {
                "axis_direction_x": controls[0].value(), "axis_direction_y": controls[1].value(), "axis_direction_z": controls[2].value(),
                "axis_point_x": controls[3].value(), "axis_point_y": controls[4].value(), "axis_point_z": controls[5].value(),
            }
        return True

    def _read_step(self) -> Step:
        op = self.op_combo.currentText()
        forced = self.force.isChecked()
        if op == "filter":
            name = self._controls[0].currentText()
            return Step(op, {"name": name, "value": self._read_value()}, forced)
        if op == "extreme":
            return Step(op, {
                "metric": self._controls[0].currentText(),
                "direction": self._controls[1].currentText(),
                "tolerance": self._controls[2].value(),
            }, forced)
        if op == "sort_take":
            return Step(op, {
                "metric": self._controls[0].currentText(),
                "direction": self._controls[1].currentText(),
                "count": self._controls[2].value(),
            }, forced)
        return Step(op, {"count": self._controls[0].value()}, forced)

    def _op_changed(self, operation: str):
        if self._building:
            return
        self.step = self._default_step(operation)
        self._rebuild_args()
        self.changed.emit()

    @staticmethod
    def _default_filter_value(name: str) -> Any:
        if name == "geom_type":
            return "PLANE"
        if name in BOOL_PREDICATES:
            return True
        if name in AXIS_PREDICATES:
            return "Z"
        if name == "convexity":
            return "concave"
        if name == "metric_range":
            return {"metric": "radius", "min": 0.0, "max": 10.0, "tolerance": 1e-6}
        if name == "bbox_touch":
            return {"axis": "Z", "side": "max", "tolerance": 1e-6}
        if name == "center_on_axis":
            return {"axis": "Z", "tolerance": 1e-6}
        if name == "angle_to_axis":
            return {"axis": "Z", "value": 0.0, "tolerance": 1e-5}
        if name in {"metric_equal", "metric_compare"}:
            return {"metric": "z", "operator": "==", "value": 0.0, "tolerance": 1e-6}
        if name == "count_compare":
            return {"metric": "face_count", "operator": "==", "value": 1.0, "tolerance": 0.0}
        return True

    def _predicate_changed(self, name: str):
        if self._building:
            return
        self.step = Step("filter", {"name": str(name), "value": self._default_filter_value(str(name))}, self.force.isChecked())
        self._rebuild_args()
        self.changed.emit()

    def _args_changed(self, *_args):
        if self._building:
            return
        self.step = self._read_step()
        self.changed.emit()

    def _force_changed(self, _checked: bool):
        if self._building:
            return
        self.step = self._read_step()
        self._update_force_indicator()
        self.changed.emit()

    @staticmethod
    def _default_step(operation: str) -> Step:
        if operation == "filter":
            return Step("filter", {"name": "planar", "value": True}, True)
        if operation == "extreme":
            return Step("extreme", {"metric": "z", "direction": "max", "tolerance": 1e-6}, True)
        if operation == "sort_take":
            return Step("sort_take", {"metric": "z", "direction": "max", "count": 1}, True)
        return Step(operation, {"count": 1}, True)

    def current_step(self) -> Step:
        return self._read_step()

    def set_result(self, text: str, tooltip: str = ""):
        self.result_label.setText(text)
        if tooltip:
            self.result_label.setToolTip(tooltip)


PRESETS = [
    ("⚡ Quick Presets…", None),
    ("Top Face (+Z)", ("extreme", {"metric": "z", "direction": "max", "tolerance": 1e-6})),
    ("Bottom Face (-Z)", ("extreme", {"metric": "z", "direction": "min", "tolerance": 1e-6})),
    ("Front Face (-Y)", ("extreme", {"metric": "y", "direction": "min", "tolerance": 1e-6})),
    ("Back Face (+Y)", ("extreme", {"metric": "y", "direction": "max", "tolerance": 1e-6})),
    ("Left Face (-X)", ("extreme", {"metric": "x", "direction": "min", "tolerance": 1e-6})),
    ("Right Face (+X)", ("extreme", {"metric": "x", "direction": "max", "tolerance": 1e-6})),
    ("All Internal Fillet Edges (Concave)", ("filter", {"name": "convexity", "value": "concave"})),
    ("All External Chamfer Edges (Convex)", ("filter", {"name": "convexity", "value": "convex"})),
    ("All Smooth / Tangent Edges", ("filter", {"name": "convexity", "value": "smooth"})),
    ("All Vertical Edges (|| Z)", ("filter", {"name": "axis_parallel", "value": "Z"})),
    ("All Horizontal Edges (⊥ Z)", ("filter", {"name": "axis_perpendicular", "value": "Z"})),
    ("All Cylindrical Faces", ("filter", {"name": "cylindrical", "value": True})),
    ("All Planar Faces", ("filter", {"name": "planar", "value": True})),
    ("All Outer Boundary Edges", ("filter", {"name": "boundary", "value": True})),
    ("All Internal Manifold Edges", ("filter", {"name": "manifold_edge", "value": True})),
    ("Holes by Diameter (M3-M8 Range)", ("filter", {"name": "metric_range", "value": {"metric": "diameter", "min": 3.0, "max": 8.5}})),
    ("Largest Area Face", ("extreme", {"metric": "area", "direction": "max", "tolerance": 1e-6})),
    ("Smallest Area Face", ("extreme", {"metric": "area", "direction": "min", "tolerance": 1e-6})),
    ("Longest Edge", ("extreme", {"metric": "length", "direction": "max", "tolerance": 1e-6})),
    ("Shortest Edge", ("extreme", {"metric": "length", "direction": "min", "tolerance": 1e-6})),
]


# ---------------------------------------------------------------------------
#  FeatureSelectorPanel – streamlined progressive-disclosure layout
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
        self.source_obj = None
        self.kind: Optional[str] = None
        self.target_subnames: list[str] = []
        self.captured_selection: list[str] = []
        self.plans: list[Plan] = []
        self.active_plan_index = -1
        self.steps: list[Step] = []
        self.selector_obj = None
        self._updating = False
        self._dialog_open = False
        # Background planning state: every planning submission bumps the
        # generation; slots drop results from superseded generations (logged,
        # never silently applied).  The full alternative list is computed
        # lazily on section expand; _routes_complete tracks whether the listed
        # plans are exhaustive for the current _routes_fingerprint.
        self._plan_generation = 0
        self._plan_task = None
        self._routes_complete = False
        self._routes_fingerprint = None
        self._audit_generation = 0
        self._audit_task = None
        self._build_ui()

    # ------------------------------------------------------------------
    #  UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Feature Selector will be a Task Panel
        # We don't create a QDockWidget anymore

        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Status line ──
        self.status = QtWidgets.QLabel("Select geometry, then press 🛡 Robust Selection…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # ── Diagnostics banner (hidden by default) ──
        self.diagnostics_label = QtWidgets.QLabel("")
        self.diagnostics_label.setWordWrap(True)
        self.diagnostics_label.setVisible(False)
        layout.addWidget(self.diagnostics_label)

        # ── Action bar: Capture + Live preview + Quick Guide ──
        action_bar = QtWidgets.QHBoxLayout()
        action_bar.setSpacing(4)
        self.capture_btn = QtWidgets.QPushButton("🛡 Robust Selection…")
        self.capture_btn.setDefault(True)
        self.capture_btn.setToolTip("Capture the current FreeCAD selection and plan semantic routes (S, R).")
        self.capture_btn.clicked.connect(self.learn)
        action_bar.addWidget(self.capture_btn, 2)
        self.live_preview_cb = QtWidgets.QCheckBox("Preview")
        self.live_preview_cb.setChecked(True)
        self.live_preview_cb.setToolTip("Live 3D highlight of matching subelements.")
        self.live_preview_cb.toggled.connect(self._toggle_live_preview)
        action_bar.addWidget(self.live_preview_cb)
        self.help_btn = QtWidgets.QToolButton()
        self.help_btn.setText("?")
        self.help_btn.setToolTip("Feature Selector quick guide & shortcuts")
        self.help_btn.clicked.connect(self.show_quick_guide)
        action_bar.addWidget(self.help_btn)
        layout.addLayout(action_bar)

        # ── Source summary (compact 1-line) ──
        self.source_label = QtWidgets.QLabel("—")
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color: palette(bright-text); padding: 2px;")
        layout.addWidget(self.source_label)

        # ── Selector Chain (always visible, compact) ──
        chain_frame = QtWidgets.QFrame()
        chain_frame.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        chain_layout = QtWidgets.QVBoxLayout(chain_frame)
        chain_layout.setContentsMargins(4, 4, 4, 4)
        chain_layout.setSpacing(2)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.rows_container = QtWidgets.QWidget()
        self.rows_layout = QtWidgets.QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(1)
        self.rows_layout.addStretch(1)
        scroll.setWidget(self.rows_container)
        scroll.setMinimumHeight(40)
        scroll.setMaximumHeight(180)
        chain_layout.addWidget(scroll, 1)

        # Add step + presets row
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(3)
        self.insert_btn = QtWidgets.QPushButton("+ Add")
        self.insert_btn.setToolTip("Append a new filter step to the chain.")
        self.insert_btn.clicked.connect(lambda: self._insert_step(len(self.steps), None))
        add_row.addWidget(self.insert_btn)
        self.preset_combo = QtWidgets.QComboBox()
        for label, _data in PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.preset_combo.setToolTip("Insert a common engineering query.")
        add_row.addWidget(self.preset_combo, 1)
        chain_layout.addLayout(add_row)

        # Result summary
        self.current_result = QtWidgets.QLabel("Result: —")
        self.current_result.setWordWrap(True)
        chain_layout.addWidget(self.current_result)

        layout.addWidget(chain_frame, 2)

        # ── Primary action button (prominent) ──
        self.save_btn = QtWidgets.QPushButton("Create Robust Selector")
        self.save_btn.setToolTip("Create the robust selector feature.")
        self.save_btn.clicked.connect(self.save_selector)
        bind_font = self.save_btn.font()
        bind_font.setBold(True)
        self.save_btn.setFont(bind_font)
        self.save_btn.setMinimumHeight(30)
        self.save_btn.setStyleSheet(
            "QPushButton { padding: 6px; }"
        )
        layout.addWidget(self.save_btn)

        # ── Secondary actions (compact) ──
        secondary = QtWidgets.QHBoxLayout()
        secondary.setSpacing(3)
        self.preview_btn = QtWidgets.QPushButton("Preview")
        self.preview_btn.setToolTip("Apply result to FreeCAD's normal selection (non-persistent).")
        self.preview_btn.clicked.connect(self.preview_selection)
        secondary.addWidget(self.preview_btn)
        self.refresh_btn = QtWidgets.QPushButton("Replan")
        self.refresh_btn.setToolTip("Rebuild semantic routes keeping forced steps.")
        self.refresh_btn.clicked.connect(self.replan)
        secondary.addWidget(self.refresh_btn)
        layout.addLayout(secondary)

        # ── Collapsible: Alternative Routes ──
        self.routes_section = _CollapsibleSection("Alternative Routes", collapsed=True, parent=root)
        routes_content = QtWidgets.QWidget()
        routes_layout = QtWidgets.QVBoxLayout(routes_content)
        routes_layout.setContentsMargins(2, 2, 2, 2)
        self.route_tree = QtWidgets.QTreeWidget()
        self.route_tree.setHeaderLabels(["Route", "Steps"])
        self.route_tree.setAlternatingRowColors(True)
        self.route_tree.setMinimumHeight(80)
        self.route_tree.setMaximumHeight(200)
        self.route_tree.itemSelectionChanged.connect(self._route_selected)
        routes_layout.addWidget(self.route_tree)
        self.routes_section.setContentWidget(routes_content)
        self.routes_section.expanded.connect(self._ensure_alternative_routes)
        layout.addWidget(self.routes_section)

        # ── Collapsible: Candidate Details ──
        self.candidates_section = _CollapsibleSection("Candidate Details", collapsed=True, parent=root)
        cand_content = QtWidgets.QWidget()
        cand_layout = QtWidgets.QVBoxLayout(cand_content)
        cand_layout.setContentsMargins(2, 2, 2, 2)
        self.candidate_table = QtWidgets.QTableWidget()
        self.candidate_table.setColumnCount(5)
        self.candidate_table.setHorizontalHeaderLabels(["Name", "Status", "Shape", "Center", "Normal"])
        self.candidate_table.horizontalHeader().setStretchLastSection(True)
        self.candidate_table.setAlternatingRowColors(True)
        self.candidate_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidate_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.candidate_table.setMinimumHeight(60)
        self.candidate_table.setMaximumHeight(200)
        self.candidate_table.itemSelectionChanged.connect(self._candidate_row_selected)
        cand_layout.addWidget(self.candidate_table)
        self.candidates_section.setContentWidget(cand_content)
        layout.addWidget(self.candidates_section)

        # ── Collapsible: Advanced Tools ──
        self.advanced_section = _CollapsibleSection("Advanced Tools", collapsed=True, parent=root)
        adv_content = QtWidgets.QWidget()
        adv_layout = QtWidgets.QVBoxLayout(adv_content)
        adv_layout.setContentsMargins(2, 2, 2, 2)
        adv_layout.setSpacing(3)
        adv_row = QtWidgets.QHBoxLayout()
        self.copy_python_btn = QtWidgets.QPushButton("📋 Copy Python")
        self.copy_python_btn.setToolTip("Copy self-contained Python code for this selector.")
        self.copy_python_btn.clicked.connect(self.copy_python_code)
        adv_row.addWidget(self.copy_python_btn)
        self.cand_audit_btn = QtWidgets.QPushButton("Audit Doc")
        self.cand_audit_btn.setToolTip("Scan all selectors in the document and report health.")
        self.cand_audit_btn.clicked.connect(self.audit_document)
        adv_row.addWidget(self.cand_audit_btn)
        self.robustify_all_btn = QtWidgets.QPushButton("🛡 Robustify All")
        self.robustify_all_btn.setToolTip("1-click convert all fragile features in the entire document.")
        self.robustify_all_btn.clicked.connect(self.robustify_all)
        adv_row.addWidget(self.robustify_all_btn)
        adv_layout.addLayout(adv_row)
        self.advanced_section.setContentWidget(adv_content)
        layout.addWidget(self.advanced_section)

        # Bottom stretch
        layout.addStretch(1)

        # Shortcuts when panel has focus
        try:
            shortcut_capture = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+R"), root)
            shortcut_capture.activated.connect(self.learn)
            shortcut_commit = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Return"), root)
            shortcut_commit.activated.connect(self.save_selector)
        except Exception:
            pass

        # Wrap in outer scroll for very small dock sizes
        root.setLayout(layout)
        scroll_outer = QtWidgets.QScrollArea()
        scroll_outer.setWidgetResizable(True)
        scroll_outer.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll_outer.setWidget(root)
        self.form = scroll_outer

    @staticmethod
    def _task_dialog_open() -> bool:
        """Whether FreeCAD has any task dialog open (activeDialog is boolean)."""
        try:
            return bool(Gui.Control.activeDialog())
        except Exception:
            return False

    def _is_own_dialog_active(self) -> bool:
        """True when our panel is the currently open task dialog."""
        return self._task_dialog_open() and bool(getattr(self, "_dialog_open", False))

    def _confirm_close_active_task(self) -> bool:
        """Standard FreeCAD behavior: ask before closing/cancelling the previous task.

        Returns True when it is OK to proceed (no task active, the active task is
        already ours, or the user accepted closing it).
        """
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

    # ---------- lifecycle / selection ----------
    def show_panel(self):
        """Open the capture-oriented panel (no object in edit)."""
        if Gui:
            if self._is_own_dialog_active():
                # Already the active task: refresh in place, don't flicker.
                if self.has_active_selector():
                    self._set_source_label()
                    self._refresh_route_plans()
                    self._update_editor_result()
                return True
            if self._task_dialog_open():
                # Standard behavior: request to close/cancel the previous task.
                if not self._confirm_close_active_task():
                    return False
                try:
                    Gui.Control.closeDialog()
                except Exception:
                    # The previous dialog is already dead; there is nothing to
                    # preserve, so fall through to a fresh build.  If a live
                    # task really is still active, showDialog below refuses
                    # loudly instead of stacking dialogs silently.
                    pass
            self._build_ui()
            if self.has_active_selector():
                self._set_source_label()
                self._refresh_route_plans()
                self._update_editor_result()
            try:
                Gui.Control.showDialog(self)
            except RuntimeError:
                self._dialog_open = False
                self.status.setText("Another task is already active; close it first.")
                return False
            self._dialog_open = True
            return True
        return False

    def edit_selector_object(self, selector_obj: Any) -> bool:
        """Shared editor entry: load *selector_obj* and show the task panel.

        Used by ``ViewProviderSelector.setEdit`` (standard double-click flow) so
        all edit entry points reuse the same code and workflow.
        """
        try:
            shown = self.show_panel()
        except RuntimeError:
            # A native task dialog became active between confirm and show
            # (FreeCAD allows only one task dialog). Let the caller fall back
            # to the standard setEdit path instead of crashing.
            return False
        if shown is False:
            return False
        self.load_selector_object(selector_obj)
        return True

    def open_editor_for_object(self, selector_obj: Any) -> bool:
        """Open *selector_obj* in the task view via the standard edit flow."""
        if Gui:
            gui_doc = getattr(Gui, "ActiveDocument", None)
            if gui_doc is not None and hasattr(gui_doc, "setEdit"):
                if gui_doc.setEdit(selector_obj, 0):
                    return True
        return self.edit_selector_object(selector_obj)

    def _note_dialog_closed(self) -> None:
        self._dialog_open = False

    def notify_edit_closed(self) -> None:
        """Called when FreeCAD ends our edit session (unsetEdit): cleanup only."""
        self._note_dialog_closed()
        if Gui is not None:
            Gui.Selection.clearSelection()

    def close_panel(self):
        owns_dialog = False
        if Gui:
            owns_dialog = self._is_own_dialog_active()
        self._note_dialog_closed()
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
        """True when FreeCAD's current edit session belongs to our selector."""
        if self.selector_obj is None or in_edit is None:
            return False
        edited_obj = getattr(in_edit, "Object", None)
        if edited_obj is self.selector_obj:
            return True
        if edited_obj is not None and getattr(edited_obj, "Name", None) == getattr(self.selector_obj, "Name", None):
            return True
        return in_edit is getattr(self.selector_obj, "ViewObject", None)

    def accept(self):
        self.save_selector()
        # Exit through the standard edit flow so the tree/task view returns to
        # its normal state (unsetEdit is invoked when we were opened via setEdit).
        self.close_panel()
        return True

    def reject(self):
        # Standard cancel: discard edits without saving; close_panel() routes
        # through resetEdit when we own the edit session.
        try:
            if Gui is not None:
                Gui.Selection.clearSelection()
        except Exception:
            pass
        self.close_panel()
        return True

    def getStandardButtons(self):
        # Let the task panel handle buttons implicitly or we handle them in the UI.
        # Returning 0 means we use our own UI buttons (save_btn).
        return 0

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
        return Selector(self.kind, tuple(self.steps), len(self.target_subnames), "interactive")

    def _refuse_foreign_source(self, source_obj) -> bool:
        """Refuse geometry from another document with guidance; True when refused."""
        active = getattr(App, "ActiveDocument", None)
        if active is None or source_obj is None:
            return False
        source_doc = getattr(source_obj, "Document", None)
        if source_doc is not None and getattr(source_doc, "Name", None) == active.Name:
            return False
        _notify_user(
            Gui.getMainWindow(),
            "Feature Selector",
            "That geometry belongs to another document. Insert an App::Link to the "
            "external part in this document and select through the link instead.",
            "warning",
        )
        self.source_obj = None
        self.kind = None
        self.target_subnames = []
        self.status.setText("Select geometry in the active document (App::Link for external parts).")
        return True

    def learn(self):
        sources, object_only = _selection_snapshot()
        if not sources:
            if len(object_only) == 1:
                obj = object_only[0]
                # Check 1: Existing FeatureSelector object
                if hasattr(obj, "Query") and hasattr(obj, "BaseObject") and obj.BaseObject is not None:
                    self.load_selector_object(obj)
                    return
                # Check 2: Consumer feature with candidate references
                from fs_bindings import inspect_feature_references
                refs_info = inspect_feature_references(obj)
                if refs_info:
                    sub_refs = [r for r in refs_info if r.get("subnames") and r.get("subnames") != [""] and r.get("kind") != "Shape"]
                    info = sub_refs[0] if sub_refs else refs_info[0]
                    if self._refuse_foreign_source(info["source"]):
                        return
                    self.source_obj = info["source"]
                    self.kind = info["kind"]
                    self.target_subnames = info["subnames"]
                    self.captured_selection = list(info["subnames"])
                    self.selector_obj = None
                    self.steps = []
                    self.status.setText(f"Planning routes for '{obj.Label}'…")
                    self._refresh_route_plans()
                    self._set_source_label()
                    self._update_editor_result()
                    return
                # Check 3: Whole-object shape
                shape = getattr(obj, "Shape", None)
                if shape is not None and not shape.isNull():
                    if self._refuse_foreign_source(obj):
                        return
                    self.source_obj = obj
                    self.kind = "Shape"
                    self.target_subnames = [""]
                    self.captured_selection = [""]
                    self.selector_obj = None
                    self.steps = []
                    self.status.setText("Planning whole-object routes…")
                    self._refresh_route_plans()
                    self._set_source_label()
                    self._update_editor_result()
                    return
            _notify_user(
                Gui.getMainWindow(),
                "Feature Selector",
                "Select subelements (faces, edges, etc.) or a consumer feature, then press Robust Selection….",
                "info",
            )
            self.status.setText("Select geometry first, then press 🛡 Robust Selection…")
            return
        source_objects = {id(item[0]) for item in sources}
        if len(source_objects) != 1:
            _notify_user(Gui.getMainWindow(), "Feature Selector", "Select subelements from one source object at a time.", "warning")
            self.status.setText("Select subelements from one object.")
            return
        kinds = {item[2] for item in sources}
        if len(kinds) != 1:
            _notify_user(Gui.getMainWindow(), "Feature Selector", "Selected subelements must be the same kind (all Faces, all Edges, etc.).", "warning")
            self.status.setText("Mixed kinds — select all Faces or all Edges.")
            return
        self.source_obj = sources[0][0]
        if self._refuse_foreign_source(self.source_obj):
            return
        self.kind = next(iter(kinds))
        self.target_subnames = _distinct([name for _obj, names, _kind in sources for name in names])
        self.captured_selection = list(self.target_subnames)
        self.selector_obj = None
        self.steps = []
        self.save_btn.setText("Save Only")
        self.status.setText("Planning routes…")
        self._refresh_route_plans()

        self._set_source_label()
        self._update_editor_result()

    def load_selector_object(self, selector_obj: Any):
        try:
            selector = Selector.from_json(selector_obj.Query)
            source = selector_obj.BaseObject
            if source is None:
                raise ValueError("The selector has no source object")
            captured = list(getattr(selector_obj, "CapturedSelection", []) or [])
            if not captured:
                resolved = selector.evaluate_candidates(source)
                captured = [candidate.ref.subname for candidate in resolved]
            self.selector_obj = selector_obj
            self.source_obj = source
            self.kind = selector.kind
            self.target_subnames = captured
            self.captured_selection = captured
            self.steps = list(selector.steps)
            # Alternatives are computed lazily when the routes section expands.
            self.plans = []
            self.active_plan_index = -1
            self._mark_routes_stale()
            self._render_routes()

            self._set_source_label()
            self._update_editor_result()
            self.save_btn.setText("Save Changes")
        except Exception as exc:
            _notify_user(Gui.getMainWindow(), "Feature Selector", f"Could not open selector: {exc}", "warning")
            self.status.setText(f"Could not open selector: {exc}")

    # ---------- planning (background; never blocks the UI) ----------
    def _next_plan_generation(self) -> int:
        self._plan_generation += 1
        return self._plan_generation


    def _submit_planning_task(self, status_text: str, fn, apply):
        """Run ``fn`` (which must only touch detached data) in the background.

        ``apply`` runs on the GUI thread with the result.  Recomputes of the
        source document stay frozen until the task settles either way.
        """
        from fs_tasks import freeze_recomputes, unfreeze_recomputes

        gen = self._next_plan_generation()
        doc = getattr(self.source_obj, "Document", None)
        freeze_recomputes(doc)
        self.status.setText(status_text)
        task = submit_background(
            fn,
            on_done=lambda result, _gen=gen, _doc=doc: self._planning_succeeded(_gen, _doc, apply, result),
            on_failed=lambda message, _gen=gen, _doc=doc: self._planning_failed(_gen, _doc, message),
        )
        self._plan_task = task
        return task

    def _planning_succeeded(self, gen, doc, apply, result):
        from fs_tasks import unfreeze_recomputes

        unfreeze_recomputes(doc)
        if self.form is None:
            App.Console.PrintLog("FeatureSelector: dropped planning result for a closed panel.\n")
            return
        if gen != self._plan_generation:
            App.Console.PrintLog(f"FeatureSelector: dropped stale planning result (gen {gen}).\n")
            return
        apply(result)

    def _planning_failed(self, gen, doc, message):
        from fs_tasks import unfreeze_recomputes

        unfreeze_recomputes(doc)
        if self.form is None:
            App.Console.PrintLog("FeatureSelector: dropped planning failure for a closed panel.\n")
            return
        if gen != self._plan_generation:
            App.Console.PrintLog(f"FeatureSelector: dropped stale planning failure (gen {gen}).\n")
            return
        self.plans = []
        self._mark_routes_stale()
        self.status.setText(f"Planner error: {message}")
        self._render_routes()

    def _routes_fingerprint_now(self):
        steps_sig = tuple(
            (step.op, json.dumps(step.args, sort_keys=True, default=str), step.forced)
            for step in self.steps
        )
        source = self.source_obj
        return (
            getattr(getattr(source, "Document", None), "Name", ""),
            object_name(source) if source is not None else "",
            self.kind,
            tuple(self.target_subnames),
            steps_sig,
        )

    def _mark_routes_stale(self):
        self._routes_complete = False
        if hasattr(self, "routes_section"):
            self.routes_section.setTitle("Alternative Routes")

    def _mark_routes_complete(self):
        self._routes_complete = True
        if hasattr(self, "routes_section"):
            self.routes_section.setTitle(f"Alternative Routes ({len(self.plans)})")

    def _planning_inputs(self, max_results):
        """Capture detached planning inputs on the GUI thread."""
        pool = candidates(self.source_obj, self.kind)
        return {
            "pool": pool,
            "kind": self.kind,
            "targets": list(self.target_subnames),
            "depth": max(6, len(self.steps) + 3),
            "forced": {index: step for index, step in enumerate(self.steps) if step.forced},
            "max_results": max_results,
        }

    @staticmethod
    def _compute_planned_routes(inputs: dict[str, Any]) -> list[Plan]:
        from fs_planner import plan_selectors
        return plan_selectors(
            inputs["pool"],
            inputs["kind"],
            inputs["targets"],
            max_depth=inputs["depth"],
            max_results=inputs["max_results"],
            required_positions=inputs["forced"],
        )

    def _refresh_route_plans(self):
        """Plan the single best route eagerly; alternatives wait for expand."""
        if not self.source_obj or not self.kind:
            return
        try:
            inputs = self._planning_inputs(max_results=1)
        except Exception as exc:
            self.plans = []
            self._mark_routes_stale()
            self.status.setText(f"Cannot plan: {exc}")
            self._render_routes()
            return
        fingerprint = self._routes_fingerprint_now()

        def apply(plans):
            self.plans = list(plans)
            self.steps = list(self.plans[0].selector.steps) if self.plans else []
            self.active_plan_index = 0 if self.plans else -1
            if not self.steps:
                self.current_result.setText("Result: no automatic route found; add selectors manually.")
            else:
                self.status.setText("Best route planned — review the chain, expand Alternative Routes for more.")
            self._routes_fingerprint = fingerprint
            self._mark_routes_stale()
            self._render_routes()
            self._render_rows()
            self._update_editor_result()

        self._submit_planning_task("Planning best route in background…", lambda: self._compute_planned_routes(inputs), apply)

    def _ensure_alternative_routes(self):
        """Compute the full alternative list; only runs on section expand."""
        if self._routes_complete and self._routes_fingerprint == self._routes_fingerprint_now():
            return
        if not self.source_obj or not self.kind or not self.target_subnames:
            return
        try:
            inputs = self._planning_inputs(max_results=12)
        except Exception as exc:
            self.status.setText(f"Cannot plan: {exc}")
            return
        fingerprint = self._routes_fingerprint_now()

        def apply(plans):
            if not plans:
                self.status.setText("No alternative routes found.")
                return
            current = self.current_selector()
            current_json = current.to_json() if current is not None else None
            self.plans = list(plans)
            self.active_plan_index = next(
                (i for i, plan in enumerate(plans) if plan.selector.to_json() == current_json),
                0,
            )
            self._routes_fingerprint = fingerprint
            self._mark_routes_complete()
            self._render_routes()
            self.status.setText(f"{len(plans)} routes computed — pick one to edit the chain.")

        self._submit_planning_task("Computing alternative routes in background…", lambda: self._compute_planned_routes(inputs), apply)

    def replan(self):
        if not self.source_obj or not self.kind or not self.target_subnames:
            return
        self._refresh_route_plans()
        self.status.setText("Replanned with forced selectors retained.")
        self._update_editor_result()

    def _request_completion(self, prefix_steps, forced_positions, apply):
        """Complete the edited prefix in the background; ``apply`` gets the plans."""
        if not self.source_obj or not self.kind:
            return None
        try:
            pool = candidates(self.source_obj, self.kind)
        except Exception as exc:
            self.status.setText(f"Cannot plan: {exc}")
            return None
        kind = self.kind
        targets = list(self.target_subnames)
        prefix = tuple(prefix_steps)
        forced = dict(forced_positions)

        def compute():
            from fs_planner import complete_selector
            return complete_selector(
                pool,
                kind,
                targets,
                prefix_steps=prefix,
                forced_positions=forced,
                max_added_depth=6,
                max_results=1,
            )

        self.status.setText("Replanning downstream in background…")
        return self._submit_planning_task("Replanning downstream in background…", compute, apply)

    def _complete_after_edit(self, edited_index: int):
        if not self.source_obj or not self.kind:
            return
        self.steps = [row.current_step() for row in self._rows()]
        prefix = tuple(self.steps[: edited_index + 1])
        forced_positions = {
            index: step for index, step in enumerate(self.steps)
            if index > edited_index and step.forced
        }

        def apply(completed):
            if completed:
                chosen = completed[0].selector
                self.steps = list(chosen.steps)
                self.status.setText(f"Updated step {edited_index + 1}; replanned downstream.")
            else:
                self.status.setText("No exact completion — edit or add another step.")
            current = self.current_selector()
            current_json = current.to_json() if current is not None else None
            self.active_plan_index = next(
                (i for i, plan in enumerate(self.plans) if plan.selector.to_json() == current_json), -1
            )
            self._mark_routes_stale()
            self._render_rows()
            self._update_editor_result()

        self._request_completion(prefix, forced_positions, apply)

    # ---------- route / row UI ----------
    def _render_routes(self):
        self._updating = True
        try:
            self.route_tree.clear()
            for index, plan in enumerate(self.plans):
                root = QtWidgets.QTreeWidgetItem([f"{index + 1}. {plan.explanation}", str(plan.score[0])])
                root.setData(0, QtCore.Qt.ItemDataRole.UserRole, index)
                self.route_tree.addTopLevelItem(root)
                for step in plan.selector.steps:
                    label = ("📌 " if step.forced else "• ") + step.label()
                    child = QtWidgets.QTreeWidgetItem([label, "pinned" if step.forced else "auto"])
                    root.addChild(child)
                root.setExpanded(True)
            if self.active_plan_index >= 0 and self.active_plan_index < self.route_tree.topLevelItemCount():
                self.route_tree.setCurrentItem(self.route_tree.topLevelItem(self.active_plan_index))
        finally:
            self._updating = False

    def _route_selected(self):
        if self._updating:
            return
        item = self.route_tree.currentItem()
        if item is None:
            return
        root = item if item.parent() is None else item.parent()
        index = root.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if index is None:
            return
        index = int(index)
        if not (0 <= index < len(self.plans)):
            return
        self.active_plan_index = index
        self.steps = list(self.plans[index].selector.steps)
        self._render_rows()
        self._update_editor_result()

    def _rows(self) -> list[StepRow]:
        return [
            self.rows_layout.itemAt(i).widget()
            for i in range(self.rows_layout.count() - 1)
            if isinstance(self.rows_layout.itemAt(i).widget(), StepRow)
        ]

    def _render_rows(self):
        self._updating = True
        try:
            while self.rows_layout.count() > 1:
                item = self.rows_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            for index, step in enumerate(self.steps):
                row = StepRow(step, index, self.rows_container)
                row.changed.connect(lambda i=index: self._row_changed(i))
                row.move_requested.connect(lambda delta, i=index: self._move_step(i, delta))
                row.remove_requested.connect(lambda i=index: self._remove_step(i))
                row.insert_requested.connect(lambda i=index: self._insert_step(i, None))
                self.rows_layout.insertWidget(index, row)
            self._update_row_results()
        finally:
            self._updating = False

    def _row_changed(self, index: int):
        if self._updating:
            return
        self._complete_after_edit(index)

    def _move_step(self, index: int, delta: int):
        new_index = index + delta
        if not (0 <= index < len(self.steps) and 0 <= new_index < len(self.steps)):
            return
        rows = self._rows()
        self.steps = [row.current_step() for row in rows]
        self.steps[index], self.steps[new_index] = self.steps[new_index], self.steps[index]
        self._complete_after_edit(min(index, new_index))

    def _remove_step(self, index: int):
        self.steps = [row.current_step() for row in self._rows()]
        if 0 <= index < len(self.steps):
            self.steps.pop(index)
        prefix = tuple(self.steps[:index])
        forced_positions = {
            row_index: step for row_index, step in enumerate(self.steps[index:], start=index)
            if step.forced
        }

        def apply(completed):
            if completed:
                self.steps = list(completed[0].selector.steps)
            self._mark_routes_stale()
            self._render_rows()
            self._update_editor_result()

        if self.source_obj and self.kind and self.target_subnames:
            self._request_completion(prefix, forced_positions, apply)
        else:
            self._mark_routes_stale()
            self._render_rows()
            self._update_editor_result()

    def _insert_step(self, index: int, step: Optional[Step] = None):
        new = step if step is not None else StepRow._default_step("filter")
        self.steps = [row.current_step() for row in self._rows()]
        index = max(0, min(index, len(self.steps)))
        self.steps.insert(index, new)
        self._render_rows()
        self._update_editor_result()
        if step is not None:
            self._complete_after_edit(index)
        else:
            self.status.setText("Added step — edit it, then downstream will auto-replan.")

    def _update_row_results(self):
        if not self.source_obj or not self.kind:
            return
        rows = self._rows()
        current: list[Any] = []
        try:
            from fs_selector import candidates, apply_step
            current = candidates(self.source_obj, self.kind)
            for index, row in enumerate(rows):
                try:
                    current = apply_step(current, row.current_step())
                    row.set_result(str(len(current)), f"{len(current)} candidate(s) after {row.current_step().label()}")
                except Exception as exc:
                    row.set_result("ERR", str(exc))
        except Exception:
            for row in rows:
                row.set_result("—")

    def _update_editor_result(self):
        self._updating = True
        try:
            self._update_row_results()
            selector = self.current_selector()
            if selector is None or not self.source_obj:
                self.current_result.setText("Result: —")
                self._update_candidate_table([])
                self._update_diagnostics_banner(None)
                return
            try:
                resolved = selector.evaluate_candidates(self.source_obj)
                names = {candidate.ref.subname for candidate in resolved}
                target_names = set(self.target_subnames)
                exact = names == target_names
                if exact:
                    self.current_result.setText(f"✓ {len(resolved)} feature(s) — EXACT MATCH")
                    self.current_result.setStyleSheet("font-weight: bold; color: #44cc44;")
                else:
                    self.current_result.setText(f"⚠ {len(resolved)} feature(s) — not exact ({len(target_names)} target)")
                    self.current_result.setStyleSheet("font-weight: bold; color: #ccaa44;")
                self.current_result.setToolTip(selector.describe())
                self._update_candidate_table(resolved)
                self._update_diagnostics_banner(selector)
                if getattr(self, "live_preview_cb", None) and self.live_preview_cb.isChecked():
                    self._update_3d_preview()
            except Exception as exc:
                self.current_result.setText(f"Result: error — {exc}")
                self.current_result.setStyleSheet("color: #cc4444;")
                self._update_candidate_table([])
                self._update_diagnostics_banner(None, error=str(exc))
        finally:
            self._updating = False

    def _toggle_live_preview(self, checked: bool):
        if checked:
            self._update_3d_preview()
        else:
            try:
                if Gui is not None:
                    Gui.Selection.clearSelection()
            except Exception:
                pass

    def _update_3d_preview(self):
        if not getattr(self, "live_preview_cb", None) or not self.live_preview_cb.isChecked():
            return
        refs = self._preview_refs()
        if refs:
            add_selection(refs, clear=True)

    def _preset_chosen(self, index: int):
        if self._updating or index <= 0 or index >= len(PRESETS):
            return
        preset = PRESETS[index]
        data = preset[1]
        if not data:
            return
        op, args = data
        step = Step(op, dict(args), forced=True)
        self.preset_combo.setCurrentIndex(0)
        self._insert_step(len(self.steps), step)

    def _candidate_row_selected(self):
        if self._updating or not self.source_obj:
            return
        row = self.candidate_table.currentRow()
        if row < 0:
            return
        item = self.candidate_table.item(row, 0)
        if item is None:
            return
        subname = item.text()
        from fs_freecad import FeatureRef, add_selection, shape_type_from_subname
        ref = FeatureRef(self.source_obj.Name, subname, shape_type_from_subname(subname) or "Shape")
        add_selection([ref], clear=True)

    def _update_candidate_table(self, candidates_list: list[Any]):
        self._updating = True
        try:
            self.candidate_table.setRowCount(0)
            self.candidate_table.setRowCount(len(candidates_list))
            # Update section title with count
            if hasattr(self, 'candidates_section'):
                self.candidates_section.setTitle(f"Candidate Details ({len(candidates_list)})")
            captured_set = set(self.captured_selection)
            for row_idx, cand in enumerate(candidates_list):
                subname = str(getattr(getattr(cand, "ref", None), "subname", "") or "")
                if subname in captured_set:
                    status = "✓ Exact"
                elif self.selector_obj and getattr(self.selector_obj, "TopologyRenumbered", False):
                    status = "⚡ Renumbered"
                else:
                    status = "Matched"

                geom_type = str(getattr(cand, "geom_type", "") or "").capitalize()
                area = float(getattr(cand, "area", 0.0) or 0.0)
                length = float(getattr(cand, "length", 0.0) or 0.0)
                convexity = getattr(cand, "convexity", None)
                conv_tag = f" [{convexity.title()}]" if convexity else ""
                diameter = getattr(cand, "diameter", None)
                dia_tag = f", ⌀{diameter:.2f}mm" if diameter is not None and math.isfinite(diameter) else ""
                if area > 0:
                    metric_str = f"{geom_type}{conv_tag} ({area:.2f} mm²{dia_tag})"
                elif length > 0:
                    metric_str = f"{geom_type}{conv_tag} ({length:.2f} mm{dia_tag})"
                else:
                    metric_str = f"{geom_type}{conv_tag}{dia_tag}"

                center = getattr(cand, "center", None)
                if center is not None and len(center) >= 3:
                    center_str = f"({center[0]:.2f}, {center[1]:.2f}, {center[2]:.2f})"
                else:
                    center_str = "—"

                direction = getattr(cand, "direction", None)
                if direction is not None and len(direction) >= 3:
                    dir_str = f"({direction[0]:.2f}, {direction[1]:.2f}, {direction[2]:.2f})"
                else:
                    dir_str = "—"

                self.candidate_table.setItem(row_idx, 0, QtWidgets.QTableWidgetItem(subname))
                self.candidate_table.setItem(row_idx, 1, QtWidgets.QTableWidgetItem(status))
                self.candidate_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(metric_str))
                self.candidate_table.setItem(row_idx, 3, QtWidgets.QTableWidgetItem(center_str))
                self.candidate_table.setItem(row_idx, 4, QtWidgets.QTableWidgetItem(dir_str))
        finally:
            self._updating = False

    def _update_diagnostics_banner(self, selector: Optional[Selector], error: str = ""):
        if error:
            self.diagnostics_label.setText(f"⚠ {error}")
            self.diagnostics_label.setStyleSheet("background-color: #551111; color: #ff9999; padding: 4px; border-radius: 3px;")
            self.diagnostics_label.setVisible(True)
            return

        if self.selector_obj is not None:
            renumbered = bool(getattr(self.selector_obj, "TopologyRenumbered", False))
            audit = str(getattr(self.selector_obj, "RenumberingAudit", "") or "")
            health = str(getattr(self.selector_obj, "HealthStatus", "OK") or "OK")
            res_time = float(getattr(self.selector_obj, "ResolutionTimeMs", 0.0) or 0.0)

            if renumbered:
                self.diagnostics_label.setText(f"⚡ Renumbering OK ({res_time:.1f}ms)")
                self.diagnostics_label.setStyleSheet("background-color: #554411; color: #ffdd77; padding: 4px; border-radius: 3px;")
                self.diagnostics_label.setToolTip(audit)
                self.diagnostics_label.setVisible(True)
            elif health == "Warning":
                self.diagnostics_label.setText(f"⚠ {audit}")
                self.diagnostics_label.setStyleSheet("background-color: #553311; color: #ffaa55; padding: 4px; border-radius: 3px;")
                self.diagnostics_label.setVisible(True)
            elif health == "OK":
                self.diagnostics_label.setText(f"✓ Healthy · {res_time:.1f}ms")
                self.diagnostics_label.setStyleSheet("background-color: #113311; color: #88ee88; padding: 4px; border-radius: 3px;")
                self.diagnostics_label.setVisible(True)
            else:
                self.diagnostics_label.setVisible(False)
        else:
            self.diagnostics_label.setVisible(False)

    def audit_document(self):
        from fs_tasks import freeze_recomputes, unfreeze_recomputes

        doc = getattr(App, "ActiveDocument", None)
        if doc is None:
            self.status.setText("No active document.")
            return
        try:
            doc_name, items = collect_audit_snapshots(doc)
        except Exception as exc:
            self.status.setText(f"Audit failed: {exc}")
            return
        if not items:
            self.status.setText("Audit: no robust selectors in this document.")
            return
        gen = self._next_audit_generation()
        freeze_recomputes(doc)
        self.status.setText(f"Auditing {len(items)} selector(s) in background…")
        task = submit_background(
            lambda: evaluate_audit_snapshots(items),
            on_done=lambda rows, _gen=gen, _doc=doc, _name=doc_name: self._audit_succeeded(_gen, _doc, _name, rows),
            on_failed=lambda message, _gen=gen, _doc=doc: self._audit_failed(_gen, _doc, message),
        )
        self._audit_task = task

    def _audit_succeeded(self, gen, doc, doc_name, rows):
        from fs_tasks import unfreeze_recomputes

        unfreeze_recomputes(doc)
        if self.form is None:
            App.Console.PrintLog("FeatureSelector: dropped audit result for a closed panel.\n")
            return
        if gen != self._audit_generation:
            App.Console.PrintLog("FeatureSelector: dropped stale audit result.\n")
            return
        audit = assemble_audit_summary(doc_name, rows)
        report = format_audit_report(audit)
        if hasattr(App, "Console"):
            App.Console.PrintMessage(report + "\n")
        self.status.setText(
            f"Audit: {audit['total']} selectors ({audit['healthy']} OK, "
            f"{audit['renumbered']} renumbered, {audit['warnings']} warn, {audit['errors']} err)."
        )

    def _audit_failed(self, gen, doc, message):
        from fs_tasks import unfreeze_recomputes

        unfreeze_recomputes(doc)
        if self.form is None:
            App.Console.PrintLog("FeatureSelector: dropped audit failure for a closed panel.\n")
            return
        if gen != self._audit_generation:
            App.Console.PrintLog("FeatureSelector: dropped stale audit failure.\n")
            return
        self.status.setText(f"Audit failed: {message}")

    def _next_audit_generation(self) -> int:
        self._audit_generation += 1
        return self._audit_generation

    def copy_python_code(self):
        selector = self.current_selector()
        if selector is None:
            self.status.setText("No selector to export.")
            return
        from fs_selector import selector_to_python
        obj_name = self.source_obj.Name if self.source_obj is not None else "obj"
        code = selector_to_python(selector, obj_name)
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(code)
        if hasattr(App, "Console"):
            App.Console.PrintMessage("\n" + code + "\n")
        self.status.setText("✓ Python code copied to clipboard!")

    def robustify_all(self):
        doc = getattr(App, "ActiveDocument", None)
        if doc is None:
            self.status.setText("No active document.")
            return
        from fs_bindings import format_robustify_all_report, robustify_all_features
        summary = robustify_all_features(doc)
        report = format_robustify_all_report(summary)
        if hasattr(App, "Console"):
            App.Console.PrintMessage(report + "\n")
        self.status.setText(
            f"✓ Robustified {summary['robustified_count']} features in {summary['time_ms']:.1f}ms "
            f"({summary['skipped_count']} skipped, {summary['failed_count']} failed)."
        )

    # ---------- target property UI ----------
    def _populate_target_objects(self, preferred: list[Any]):
        pass
        try:
            self.target_combo.clear()
            self.target_combo.addItem("— select target —", "")
            doc = App.ActiveDocument
            if doc is None:
                return
            ordered = []
            seen = set()
            for obj in preferred:
                if obj is None or obj is self.source_obj or id(obj) in seen:
                    continue
                ordered.append(obj); seen.add(id(obj))

            if self.selector_obj is not None:
                for record in read_binding_records(self.selector_obj):
                    t_name = str(record.get("target", ""))
                    t_obj = doc.getObject(t_name) if t_name else None
                    if t_obj and id(t_obj) not in seen:
                        ordered.append(t_obj)
                        seen.add(id(t_obj))

            for obj in getattr(doc, "Objects", []):
                if obj is self.source_obj or id(obj) in seen:
                    continue
                if target_property_candidates(obj, self.source_obj):
                    ordered.append(obj); seen.add(id(obj))
            for obj in ordered:
                self.target_combo.addItem(f"{obj.Label}  [{obj.Name}]", obj.Name)
            if ordered:
                self.target_combo.setCurrentIndex(1)
                # Auto-expand target section if a target was found
                if hasattr(self, 'target_section'):
                    self.target_section.expand()
        finally:
            self._updating = False
        if not candidates:
            self.property_status.setText("No supported writable property found.")
            self.property_status.setVisible(True)
            return

        if self.selector_obj is not None:
            for record in read_binding_records(self.selector_obj):
                if record.get("target") == target.Name:
                    b_prop = str(record.get("property", ""))
                    p_idx = self.property_combo.findData(b_prop)
                    if p_idx >= 0:
                        self.property_combo.setCurrentIndex(p_idx)
                        break

        matches = find_matching_property(target, self._preview_refs())
        if matches:
            names = [name for name, _mode in matches]
            for idx in range(self.property_combo.count()):
                if self.property_combo.itemData(idx) in names:
                    self.property_combo.setCurrentIndex(idx)
                    break
        self.property_status.setVisible(False)

    def _preview_refs(self):
        selector = self.current_selector()
        if selector is None or not self.source_obj:
            return []
        try:
            return selector.evaluate(self.source_obj)
        except Exception:
            return []

    def _exact_selector(self) -> Optional[Selector]:
        selector = self.current_selector()
        if selector is None:
            return None
        try:
            resolved = selector.evaluate_candidates(self.source_obj)
        except Exception as exc:
            self.status.setText(f"Evaluation failed: {exc}")
            return None
        resolved_names = {c.ref.subname for c in resolved}
        target_names = set(self.target_subnames)
        if resolved_names != target_names:
            if selector.expected_count is not None and len(resolved) == selector.expected_count:
                return selector
            self.status.setText("Selector must match captured subelements before committing.")
            return None
        return selector

    def preview_selection(self):
        selector = self._exact_selector()
        if selector is None:
            return
        applied = add_selection(selector.evaluate(self.source_obj), clear=True)
        self.status.setText(f"Preview: {len(applied)} subelement(s) selected.")

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

    def _save_object_only(self, selector: Selector):
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
            return
        doc = App.ActiveDocument
        if doc is None:
            return
        try:
            def operation():
                obj = self._save_object_only(selector)
                self.selector_obj = obj
                return obj
            obj = self._transaction(doc, "Create robust feature selector", operation)
            self.status.setText(f"✓ Saved {obj.Label}")
            self.save_btn.setText("Save Changes")
            # Seamless native workflow: leave the robust result in FreeCAD's normal
            # global selection so the next placement/operation task (Fillet/Chamfer
            # picker, Sketch attachment, DatumPlane, ...) can consume it directly.
            if self.source_obj is not None:
                add_selection(selector.evaluate(self.source_obj), clear=True)
        except Exception as exc:
            _notify_user(Gui.getMainWindow(), "Feature Selector", f"Could not save:\n{exc}", "critical")
            self.status.setText(f"Save failed: {exc}")

    def _set_source_label(self):
        if not self.source_obj:
            self.source_label.setText("—")
            return
        count = len(self.target_subnames)
        names = ", ".join(self.target_subnames[:5])
        if count > 5:
            names += f" +{count - 5} more"
        self.source_label.setText(f"{self.source_obj.Label} · {count} {self.kind}(s): {names}")

    def show_quick_guide(self):
        import os
        if os.environ.get("FREECAD_NO_POPUP", "").strip().lower() in {"1", "true", "yes"}:
            if hasattr(App, "Console"):
                App.Console.PrintMessage("Feature Selector Quick Guide: S,R = Robust Selection, S,F = Robustify Feature, S,A = Robustify All.\n")
            return
        text = (
            "<h3>Feature Selector Quick Guide</h3>"
            "<p>Feature Selector creates <b>explicit semantic selections</b> that survive parametric changes and topological renumbering.</p>"
            "<h4>Standard Workflow:</h4>"
            "<ol>"
            "<li><b>Select geometry</b> in the 3D view (faces, edges, etc.).</li>"
            "<li>Click <b>🛡 Robust Selection…</b> (or press <code>S, R</code>).</li>"
            "<li>Review the planned rule, then click <b>Create Robust Selector</b> (or <b>Create + Bind</b>).</li>"
            "</ol>"
            "<h4>1-Click Feature Robustification:</h4>"
            "<ul>"
            "<li>Select an existing Fillet/Chamfer/Binder, click <b>Robustify Selected Feature</b> (<code>S, F</code>).</li>"
            "<li>Or click <b>Robustify Entire Document</b> (<code>S, A</code>) to protect all features.</li>"
            "</ul>"
            "<h4>Customizing Rules:</h4>"
            "<ul>"
            "<li><b>Right-click</b> any step to pin as design intent (📌), move, or delete.</li>"
            "<li>Use the <b>Presets</b> dropdown to insert common engineering queries.</li>"
            "<li>Expand <b>Alternative Routes</b> to pick another rule formulation.</li>"
            "</ul>"
        )
        msg = QtWidgets.QMessageBox(Gui.getMainWindow())
        msg.setWindowTitle("Feature Selector Guide")
        msg.setTextFormat(QtCore.Qt.TextFormat.RichText)
        msg.setText(text)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Information)
        msg.exec_()

    def close(self):
        self.close_panel()
