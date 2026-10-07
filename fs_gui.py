"""Streamlined, native-style FreeCAD task panel for robust feature selection.

Uses CadQuery selector expression language for concise, powerful geometric queries
with subelement descending, cursor-based autocompletion, and native FreeCAD OK/Cancel tasks.
"""
from __future__ import annotations

import json
import math
import os
import re
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
    compute_synchronized_colors,
    decompose_clauses,
    evaluate_expression,
    evaluate_expression_items,
    parse_expression,
    steps_to_expression,
    ExpressionClause,
    SYNCHRONIZED_PALETTE,
    OVERLAP_COLOR,
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
    ("Planar Faces (:planar)", ":planar"),
    ("Cylindrical Faces (:cylindrical)", ":cylindrical"),
    ("All Internal Fillet Edges (Concave)", ":concave"),
    ("All External Chamfer Edges (Convex)", ":convex"),
    ("All Smooth / Tangent Edges", ":smooth"),
    ("Holes by Diameter (M3-M8 Range)", "3.0 <= diameter <= 8.0"),
    ("Largest Radius (>>radius[0])", ">>radius[0]"),
    ("Smallest Radius (<<radius[0])", "<<radius[0]"),
    ("Longest Edges (>>length[0])", ">>length[0]"),
    ("Edges of Top Face (faces('>Z').edges())", 'faces(">Z").edges()'),
    ("Adjacent to Face 1 (adjacent_to('Face1'))", 'adjacent_to("Face1")'),
    ("Linear Edges (:linear)", ":linear"),
    ("Circular Edges (:circular)", ":circular"),
    ("Planar Faces (%Plane)", "%Plane"),
    ("Cylindrical Faces (%Cylinder)", "%Cylinder"),
    ("Intersection (and)", " and "),
    ("Union (or)", " or "),
    ("Negation (not)", "not "),
    ("Set Difference (exc)", " exc "),
]


class ExpressionHighlighter(QtGui.QSyntaxHighlighter):
    """Synchronized syntax highlighter mapping expression clauses 1:1 to 3D geometry features."""

    def __init__(self, parent: QtGui.QTextDocument):
        super().__init__(parent)
        self._clauses: list[ExpressionClause] = []
        self._active_clause_idx: Optional[int] = None
        self._rules: list[tuple[QtCore.QRegularExpression, QtGui.QTextCharFormat]] = []
        self._init_fallback_rules()

    def set_synchronized_clauses(self, clauses: list[ExpressionClause], active_idx: Optional[int] = None):
        """Update clause spans and active clause, re-evaluating syntax highlights."""
        same_clauses = len(self._clauses) == len(clauses) and all(
            c1.text == c2.text and c1.start_pos == c2.start_pos and c1.end_pos == c2.end_pos and c1.color_index == c2.color_index
            for c1, c2 in zip(self._clauses, clauses)
        )
        if same_clauses and self._active_clause_idx == active_idx:
            return
        self._clauses = list(clauses)
        self._active_clause_idx = active_idx
        self.rehighlight()

    def set_active_clause(self, active_idx: Optional[int]):
        """Update active focused clause index and refresh highlighting."""
        if self._active_clause_idx == active_idx:
            return
        self._active_clause_idx = active_idx
        self.rehighlight()

    def _init_fallback_rules(self):
        def _fmt(color: str, bold: bool = False, italic: bool = False) -> QtGui.QTextCharFormat:
            f = QtGui.QTextCharFormat()
            f.setForeground(QtGui.QColor(color))
            if bold:
                f.setFontWeight(QtGui.QFont.Weight.Bold)
            if italic:
                f.setFontItalic(True)
            return f

        self._rules.append((QtCore.QRegularExpression(r"\b(faces|edges|vertices|wires|solids)\b"), _fmt("#0277bd", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"\b(adjacent_to|coplanar_to|coaxial_to)\b"), _fmt("#00838f", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r":[A-Za-z_][A-Za-z0-9_]*"), _fmt("#2e7d32", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"=[A-Za-z0-9_][A-Za-z0-9_.]*"), _fmt("#00838f", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"(>>|<<)[A-Za-z0-9_]+(\[[^\]]*\])?"), _fmt("#6a1b9a", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"[><|#\+\-][XYZxyz]"), _fmt("#7b1fa2", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"%[A-Za-z]+"), _fmt("#ad1457", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"\b(and|or|not|exc|in)\b"), _fmt("#c2185b", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"\b(radius|rad|length|len|area|volume|vol|distance|dist|perimeter|perim|diameter|[xyzXYZ])\b"), _fmt("#1565c0")))
        self._rules.append((QtCore.QRegularExpression(r"(==|!=|<=|>=|<|>)"), _fmt("#e65100", bold=True)))
        self._rules.append((QtCore.QRegularExpression(r"\b[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?\b"), _fmt("#ef6c00")))
        self._rules.append((QtCore.QRegularExpression(r"\"[^\"]*\"|'[^']*'"), _fmt("#558b2f", italic=True)))

    def highlightBlock(self, text: str):
        if not text:
            return

        # Synchronized 1:1 clause-to-3D highlighting
        if self._clauses:
            m_wrap = re.match(r"^([A-Za-z_][A-Za-z0-9_]*\s*\(\s*[\"'])(.*)([\"']\s*\))$", text, re.DOTALL)
            if m_wrap:
                p_len = len(m_wrap.group(1))
                s_start = len(text) - len(m_wrap.group(3))
                f_dim = QtGui.QTextCharFormat()
                f_dim.setForeground(QtGui.QColor("#78909c"))
                self.setFormat(0, p_len, f_dim)
                self.setFormat(s_start, len(m_wrap.group(3)), f_dim)

            for idx, c in enumerate(self._clauses):
                start = c.start_pos
                length = c.end_pos - c.start_pos
                if start < 0 or start + length > len(text):
                    continue

                pal = SYNCHRONIZED_PALETTE[c.color_index % len(SYNCHRONIZED_PALETTE)]
                fmt = QtGui.QTextCharFormat()
                fmt.setForeground(QtGui.QColor(pal["text_dark"]))

                is_active = (self._active_clause_idx == idx)
                bg_color = QtGui.QColor(pal["hex"])
                bg_color.setAlpha(65 if is_active else 28)
                fmt.setBackground(QtGui.QBrush(bg_color))

                fmt.setFontUnderline(True)
                fmt.setUnderlineColor(QtGui.QColor(pal["border_editor"]))
                if is_active:
                    fmt.setFontWeight(QtGui.QFont.Weight.Bold)

                self.setFormat(start, length, fmt)
            return

        # Fallback rule-based highlighting
        for pattern, fmt in self._rules:
            match_iter = pattern.globalMatch(text)
            while match_iter.hasNext():
                match = match_iter.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class ExpressionEditor(QtWidgets.QPlainTextEdit):
    """QPlainTextEdit with syntax highlighting and IDE-like Tab-based auto-completion (UX-5)."""

    VOCABULARY = [
        # Methods
        'faces("', 'edges("', 'vertices("', 'wires("',
        'faces()', 'edges()', 'vertices()', 'wires()',
        # Extrema & Axes
        '>Z', '<Z', '>X', '<X', '>Y', '<Y',
        '|Z', '|X', '|Y', '#Z', '#X', '#Y',
        '+Z', '-Z', '+X', '-X', '+Y', '-Y',
        # Geometry Types
        '%Plane', '%Cylinder', '%Sphere', '%Cone', '%Torus', '%Line', '%Circle',
        # Topological & Convexity Tags
        ':concave', ':convex', ':smooth', ':closed', ':boundary', ':manifold',
        ':seam', ':hole', ':inner', ':outer', ':planar', ':cylindrical',
        ':circular', ':linear', ':spherical', ':conical', ':toroidal',
        # Canonical Metric Clustering & Slices (EXP-1, EXP-2)
        '>>radius[0]', '<<radius[0]', '>>dia[0]', '<<dia[0]',
        '>>length[0]', '<<length[0]', '>>area[0]', '<<area[0]',
        '>>Z[0]', '<<Z[0]', '>>X[0]', '<<X[0]', '>>Y[0]', '<<Y[0]',
        # Relational Combinators (EXP-5)
        'adjacent_to("', 'coplanar_to("', 'coaxial_to("',
        # Logic Keywords
        'and', 'or', 'not', 'exc', 'in',
        # Parametric Expressions
        '=VarSet.', '=Spreadsheet.',
    ]

    def __init__(self, panel: Optional[Any] = None, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.panel = panel
        self.highlighter = ExpressionHighlighter(self.document())
        self._completer = None
        self._init_completer()
        self.cursorPositionChanged.connect(self._on_cursor_position_changed)

    def _on_cursor_position_changed(self):
        if getattr(self, "_updating_cursor", False):
            return
        self._updating_cursor = True
        try:
            pos = self.textCursor().position()
            clauses = getattr(self.highlighter, "_clauses", [])
            active_idx = None
            for idx, c in enumerate(clauses):
                if c.start_pos <= pos <= c.end_pos:
                    active_idx = idx
                    break
            if self.highlighter._active_clause_idx != active_idx:
                self.highlighter.set_active_clause(active_idx)
                if self.panel and hasattr(self.panel, "_on_active_clause_changed"):
                    self.panel._on_active_clause_changed(active_idx)
        finally:
            self._updating_cursor = False

    def _init_completer(self):
        try:
            self._completer = QtWidgets.QCompleter(self.VOCABULARY, self)
            self._completer.setWidget(self)
            self._completer.setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
            self._completer.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
            self._completer.activated.connect(self._insert_completion)
        except Exception:
            self._completer = None

    def _insert_completion(self, completion: str):
        tc = self.textCursor()
        prefix = self._token_under_cursor()
        if prefix:
            move_op = getattr(QtGui.QTextCursor.MoveOperation, "Left", 1)
            keep_anchor = getattr(QtGui.QTextCursor.MoveMode, "KeepAnchor", 1)
            tc.movePosition(move_op, keep_anchor, len(prefix))
        tc.insertText(completion)
        self.setTextCursor(tc)

    def _token_under_cursor(self) -> str:
        tc = self.textCursor()
        pos = tc.position()
        text = self.toPlainText()
        start = pos
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] in ":>#<|+-%_"):
            start -= 1
        return text[start:pos]

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        key = event.key()
        key_tab = getattr(QtCore.Qt.Key, "Key_Tab", 0x01000001)
        key_enter = getattr(QtCore.Qt.Key, "Key_Enter", 0x01000004)
        key_return = getattr(QtCore.Qt.Key, "Key_Return", 0x01000005)
        key_escape = getattr(QtCore.Qt.Key, "Key_Escape", 0x01000000)

        # 1. If completer popup is visible, route navigation keys
        if self._completer and self._completer.popup() and self._completer.popup().isVisible():
            if key in (key_enter, key_return):
                self._completer.popup().hide()
                idx = self._completer.popup().currentIndex()
                if idx.isValid():
                    self._insert_completion(idx.data())
                return
            elif key == key_escape:
                self._completer.popup().hide()
                return

        # 2. Tab completion (UX-5)
        if key == key_tab:
            if self._completer and self._completer.popup():
                self._completer.popup().hide()

            cursor = self.textCursor()
            pos = cursor.position()
            text = self.toPlainText()
            prefix = text[:pos]

            # If empty or empty argument, trigger smart autocomplete
            if self.panel and (not text.strip() or prefix.endswith('("') or prefix.endswith("('")):
                self.panel.autocomplete_in_cursor()
                return

            tok = self._token_under_cursor()
            if tok:
                matches = [w for w in self.VOCABULARY if w.lower().startswith(tok.lower())]
                if matches:
                    self._insert_completion(matches[0])
                    return

            # Fallback to smart panel autocomplete if cursor is in an expression
            if self.panel:
                self.panel.autocomplete_in_cursor()
            return  # Swallow Tab without inserting literal whitespace

        super().keyPressEvent(event)


class _PreselectionObserver:
    """VIS-2: Ephemeral 3D Hover Metric Inspector observer."""

    def __init__(self, panel: Any):
        self.panel = panel

    def setPreselect(self, doc_name: str, obj_name: str, subname: str):
        if hasattr(self.panel, "_on_preselect"):
            self.panel._on_preselect(doc_name, obj_name, subname)



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
        self.tolerance: float = 1e-4
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

        self.live_preview_cb = QtWidgets.QCheckBox("Preview")
        self.live_preview_cb.setChecked(True)
        self.live_preview_cb.setToolTip("Live 3D highlight of matching subelements.")
        self.live_preview_cb.toggled.connect(self._toggle_live_preview)
        target_row.addWidget(self.live_preview_cb)
        layout.addLayout(target_row)

        # ── Row 2: CadQuery Selector Expression Editor ──
        self.expr_edit = ExpressionEditor(panel=self)
        self.expr_edit.setPlaceholderText('e.g. >Z, |Z and >Y, faces(">Z").edges("<X"), :concave, >>radius[0]')
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.expr_edit.setFont(font)
        self.expr_edit.setMinimumHeight(60)
        self.expr_edit.setMaximumHeight(90)
        self.expr_edit.textChanged.connect(self._on_expr_changed)
        layout.addWidget(self.expr_edit)

        # ── Row 3: VIS-4 Live Syntax & Match Count Ribbon Badge directly under editor ──
        self.match_ribbon = QtWidgets.QLabel("Result: —")
        self.match_ribbon.setWordWrap(True)
        self.match_ribbon.setStyleSheet("font-size: 11px; padding: 2px 6px; border-radius: 3px; color: palette(mid);")
        layout.addWidget(self.match_ribbon)

        # Compatibility aliases for tests & panel callers
        self.current_result = self.match_ribbon
        self.status = self.match_ribbon
        self.source_label = self.target_label
        self.diagnostics_label = self.match_ribbon

        # ── Row 4: Action / Helper row: Autocomplete, Presets, Tolerance, Python, Help ──
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(4)

        self.autocomplete_btn = QtWidgets.QPushButton("⚡ Autocomplete")
        self.autocomplete_btn.setToolTip("Find selector matching selection and insert at cursor position (or press Tab).")
        self.autocomplete_btn.clicked.connect(self.autocomplete_in_cursor)
        action_row.addWidget(self.autocomplete_btn)

        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.installEventFilter(NoWheelFilter(self.preset_combo))
        for label, _data in PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.preset_combo.setToolTip("Insert selector snippet at cursor.")
        action_row.addWidget(self.preset_combo, 1)

        tol_label = QtWidgets.QLabel("ε:")
        tol_label.setToolTip("Clustering & numeric comparison tolerance (default: 1e-4)")
        self.tol_combo = QtWidgets.QComboBox()
        self.tol_combo.setEditable(True)
        self.tol_combo.addItems(["1e-4", "0.001", "0.01", "0.1", "1.0", "5.0"])
        self.tol_combo.setCurrentText("1e-4")
        self.tol_combo.setMaximumWidth(70)
        self.tol_combo.setToolTip("Tolerance for metric comparisons & grouping (choose preset or enter custom value)")
        self.tol_combo.currentTextChanged.connect(self._on_tolerance_changed)
        self.tol_edit = self.tol_combo.lineEdit()
        action_row.addWidget(tol_label)
        action_row.addWidget(self.tol_combo)

        self.copy_btn = QtWidgets.QPushButton("📋 Python")
        self.copy_btn.setToolTip("Copy reproducible Python code to clipboard.")
        self.copy_btn.clicked.connect(self.copy_python_code)
        self.copy_python_btn = self.copy_btn
        action_row.addWidget(self.copy_btn)

        self.help_btn = QtWidgets.QToolButton()
        self.help_btn.setText("?")
        self.help_btn.setToolTip("CadQuery selector guide and syntax help")
        self.help_btn.clicked.connect(self.show_quick_guide)
        action_row.addWidget(self.help_btn)
        layout.addLayout(action_row)

        # ── Row 5: Compact Target Binding Controls ──
        bind_row = QtWidgets.QHBoxLayout()
        bind_row.setSpacing(4)
        bind_lbl = QtWidgets.QLabel("Target:")
        bind_row.addWidget(bind_lbl)

        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.addItem("(No target feature)", "")
        self.target_combo.currentIndexChanged.connect(self._target_feature_changed)
        bind_row.addWidget(self.target_combo, 1)

        self.property_combo = QtWidgets.QComboBox()
        self.property_combo.addItem("(Property)", "")
        bind_row.addWidget(self.property_combo, 1)

        self.bind_btn = QtWidgets.QPushButton("Create Robust Selector")
        self.bind_btn.setToolTip("Bind target property or create robust selector.")
        self.bind_btn.clicked.connect(self.bind_target_property)
        bind_row.addWidget(self.bind_btn)

        self.unbind_btn = QtWidgets.QPushButton("Unbind")
        self.unbind_btn.setToolTip("Remove binding from target property.")
        self.unbind_btn.clicked.connect(self.unbind_target_property)
        bind_row.addWidget(self.unbind_btn)
        layout.addLayout(bind_row)

        layout.addStretch(1)

        scroll_outer = QtWidgets.QScrollArea()
        scroll_outer.setWidgetResizable(True)
        scroll_outer.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll_outer.setWidget(root)
        scroll_outer.setWindowTitle("Feature Selector")
        self.form = scroll_outer

    def _set_target_combo_to_object(self, obj: Any, prop: str = ""):
        name = getattr(obj, "Name", "")
        if name and hasattr(self, "target_combo"):
            idx = self.target_combo.findData(name)
            if idx < 0:
                self.target_combo.addItem(getattr(obj, "Label", name), name)
                idx = self.target_combo.count() - 1
            self.target_combo.setCurrentIndex(idx)
            self._update_property_combo(obj)
            if prop and hasattr(self, "property_combo"):
                p_idx = self.property_combo.findData(prop)
                if p_idx >= 0:
                    self.property_combo.setCurrentIndex(p_idx)

    def use_selected_target(self):
        """Adopt selected feature from FreeCAD selection as target binding feature."""
        sources, object_only = _selection_snapshot()
        obj = None
        if sources:
            obj = sources[0][0]
        elif object_only:
            obj = object_only[0]
        if obj is not None:
            self._set_target_combo_to_object(obj)
            doc = getattr(obj, "Document", None) or (App.ActiveDocument if App else None)
            if doc:
                from fs_bindings import read_binding_records
                target_name = getattr(obj, "Name", "")
                for o in getattr(doc, "Objects", []):
                    if hasattr(o, "Query") and hasattr(o, "TargetBindings"):
                        for rec in read_binding_records(o):
                            if rec.get("target") == target_name:
                                self.selector_obj = o
                                break

    def _target_feature_changed(self, index: int):
        data = self.target_combo.itemData(index) if hasattr(self, "target_combo") else None
        if not data:
            if hasattr(self, "property_combo"):
                self.property_combo.clear()
                self.property_combo.addItem("(Property)", "")
            if hasattr(self, "bind_btn"):
                self.bind_btn.setText("Create Robust Selector")
            return
        doc = App.ActiveDocument if App else None
        target_obj = doc.getObject(data) if doc else None
        if target_obj:
            self._update_property_combo(target_obj)
            if hasattr(self, "bind_btn"):
                self.bind_btn.setText("Bind Target Property")

    def _update_property_combo(self, target_obj: Any):
        if not hasattr(self, "property_combo"):
            return
        self.property_combo.clear()
        try:
            from fs_bindings import inspect_feature_references
            refs = inspect_feature_references(target_obj)
        except Exception:
            refs = []
        if refs:
            for r in refs:
                prop = r.get("property", "")
                if prop in ("FeatureSelectorSources", "FeatureSelectorBindings") or not prop:
                    continue
                self.property_combo.addItem(f"{prop} ({r.get('kind', 'Ref')})", prop)
        else:
            for p in ("AttachmentSupport", "Base", "Edges", "Faces", "Support"):
                if hasattr(target_obj, p):
                    self.property_combo.addItem(p, p)
        if self.property_combo.count() == 0:
            self.property_combo.addItem("AttachmentSupport", "AttachmentSupport")

    def bind_target_property(self):
        target_name = self.target_combo.currentData() if hasattr(self, "target_combo") else ""
        if not target_name:
            self.save_selector()
            if hasattr(self, "status"):
                self.status.setText("Saved")
            if hasattr(self, "bind_btn"):
                self.bind_btn.setText("Save Changes")
            return
        doc = App.ActiveDocument if App else None
        target_obj = doc.getObject(target_name) if doc else None
        if not target_obj:
            return
        prop_name = self.property_combo.currentData() if hasattr(self, "property_combo") else ""
        if not prop_name and hasattr(self, "property_combo"):
            prop_name = self.property_combo.currentText().split()[0]
        if not self.selector_obj:
            self.save_selector()
        if not self.selector_obj:
            return
        try:
            from fs_bindings import bind_selector, update_selector_bindings
            bind_selector(self.selector_obj, target_obj, prop_name, mode="robust")
            selector = self._exact_selector()
            if selector and self.source_obj:
                update_selector_bindings(self.selector_obj, selector)
            if hasattr(self, "status"):
                self.status.setText(f"Bound to {target_name}.{prop_name}")
            if hasattr(self, "bind_btn"):
                self.bind_btn.setText("Save Changes")
        except Exception as exc:
            _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", f"Could not bind: {exc}", "warning")

    def unbind_target_property(self):
        target_name = self.target_combo.currentData() if hasattr(self, "target_combo") else ""
        prop_name = self.property_combo.currentData() if hasattr(self, "property_combo") else None
        if not self.selector_obj and target_name:
            doc = App.ActiveDocument if App else None
            if doc:
                from fs_bindings import read_binding_records
                for o in getattr(doc, "Objects", []):
                    if hasattr(o, "Query") and hasattr(o, "TargetBindings"):
                        for rec in read_binding_records(o):
                            if rec.get("target") == target_name:
                                self.selector_obj = o
                                break
        if self.selector_obj:
            try:
                from fs_bindings import unbind_selector, read_binding_records
                records = read_binding_records(self.selector_obj)
                target_props = {r.get("property") for r in records if r.get("target") == target_name}
                if prop_name not in target_props:
                    prop_name = None
                count = unbind_selector(self.selector_obj, target_name or None, prop_name)
                if hasattr(self, "status"):
                    self.status.setText(f"Removed {count} explicit binding(s)")
            except Exception as exc:
                _notify_user(Gui.getMainWindow() if Gui else None, "Feature Selector", f"Could not unbind: {exc}", "warning")

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
            # UX-3: Contextual feature auto-detect on panel open
            if not self.has_active_selector() and getattr(Gui, "Selection", None):
                sources, object_only = _selection_snapshot()
                if sources or object_only:
                    self.change_target()
            if self.has_active_selector():
                self._update_merged_status()
            if Gui and hasattr(Gui, "Selection") and hasattr(Gui.Selection, "addObserver"):
                try:
                    self._observer = _PreselectionObserver(self)
                    Gui.Selection.addObserver(self._observer)
                except Exception:
                    self._observer = None
            try:
                Gui.Control.showDialog(self)
            except RuntimeError:
                self._dialog_open = False
                return False
            self._dialog_open = True
            return True
        return False

    def _on_preselect(self, doc_name: str, obj_name: str, subname: str):
        """VIS-2: Ephemeral 3D Hover Metric Inspector HUD."""
        if not self.source_obj or not subname:
            return
        if getattr(self.source_obj, "Name", "") != obj_name:
            return
        kind = shape_type_from_subname(subname) or self.kind or "Shape"
        try:
            from fs_selector import candidate_for
            item = candidate_for(self.source_obj, subname, kind)
            tags = []
            if getattr(item, "convexity", None):
                tags.append(f"[{item.convexity}]")
            metrics = []
            if hasattr(item, "length") and item.length is not None and math.isfinite(item.length):
                metrics.append(f"L={item.length:.2f}mm")
            if hasattr(item, "radius") and item.radius is not None and math.isfinite(item.radius):
                metrics.append(f"⌀{item.radius*2.0:.2f}mm")
            if hasattr(item, "area") and item.area is not None and math.isfinite(item.area):
                metrics.append(f"A={item.area:.2f}mm²")
            tag_str = " ".join(tags)
            m_str = " · ".join(metrics)
            hud_text = f"{subname}: {item.geom_type.title()} {tag_str} · {m_str}".strip(" · ")
            if Gui and hasattr(Gui, "getMainWindow") and Gui.getMainWindow():
                sb = Gui.getMainWindow().statusBar()
                if sb:
                    sb.showMessage(hud_text, 2500)
        except Exception:
            pass

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
        self._clear_3color_highlighting()
        self._clear_3d_direction_indicator()
        if getattr(self, "_observer", None) and Gui and hasattr(Gui, "Selection") and hasattr(Gui.Selection, "removeObserver"):
            try:
                Gui.Selection.removeObserver(self._observer)
            except Exception:
                pass
            self._observer = None
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
        self._clear_3color_highlighting()
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
        """Update target feature(s) from active 3D selection or model tree."""
        sources, object_only = _selection_snapshot()
        if not sources:
            if len(object_only) == 1:
                obj = object_only[0]
                if hasattr(obj, "Query") and hasattr(obj, "BaseObject") and obj.BaseObject is not None:
                    self.load_selector_object(obj)
                    return
                # Check if this object is bound to an existing selector object
                doc = getattr(obj, "Document", None) or (App.ActiveDocument if App else None)
                bound_selector = None
                if doc:
                    from fs_bindings import read_binding_records
                    for o in getattr(doc, "Objects", []):
                        if hasattr(o, "Query") and hasattr(o, "TargetBindings"):
                            for rec in read_binding_records(o):
                                if rec.get("target") == getattr(obj, "Name", ""):
                                    bound_selector = o
                                    break
                if bound_selector is not None:
                    self.load_selector_object(bound_selector)
                    self._set_target_combo_to_object(obj)
                    return

                # Subelement consumer (Fillet, Chamfer, Pad, Pocket, etc.) or whole shape
                from fs_bindings import inspect_feature_references
                refs_info = inspect_feature_references(obj)
                if not refs_info:
                    from fs_bindings import _profile_sketches
                    for sk in _profile_sketches(obj):
                        refs_info = inspect_feature_references(sk)
                        if refs_info:
                            break
                if refs_info:
                    sub_refs = [r for r in refs_info if r.get("subnames") and r.get("subnames") != [""] and r.get("kind") != "Shape"]
                    info = sub_refs[0] if sub_refs else refs_info[0]
                    if self._refuse_foreign_source(info["source"]):
                        return
                    self._reset_target_state(info["source"], info["kind"], info["subnames"])
                    self._set_target_combo_to_object(obj, info.get("property", ""))
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
                "Select subelements (faces, edges, etc.) in the 3D view or a feature in the model tree, then click Change.",
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

    def _on_tolerance_changed(self):
        try:
            val = float(self.tol_edit.text().strip())
            if val > 0:
                self.tolerance = val
        except (ValueError, TypeError):
            self.tolerance = 1e-4
        self._on_expr_changed()

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
            tol = getattr(self, "tolerance", 1e-4)
            matched_items = evaluate_expression_items(self.source_obj, expr, kind=self.kind, tolerance=tol)
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
            tol = getattr(self, "tolerance", 1e-4)
            matched_items = evaluate_expression_items(self.source_obj, expr, kind=self.kind, tolerance=tol)
            self.last_resolved = matched_items
            t_ms = (time.perf_counter() - t0) * 1000.0
            matched_names = {item.subname for item in matched_items}
            m_count = len(matched_items)
            res_kind = matched_items[0].kind if matched_items else (self.kind or "Feature")

            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(m_count > 0)

            # VIS-3: Direction indicator cue
            cue = ""
            m_dir = re.search(r"([><|#+\-])([XYZ])\b", expr, re.IGNORECASE)
            if m_dir:
                cue = f" [{m_dir.group(1)}{m_dir.group(2).upper()}]"

            if self.target_subnames:
                target_set = set(self.target_subnames)
                if matched_names == target_set:
                    self.current_result.setText(f"✓ {m_count} {self.kind or res_kind}(s) (Exact match){cue} · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #2e7d32; background-color: rgba(46, 125, 50, 0.12); border-radius: 3px; padding: 2px 6px;")
                    exact_sel = Selector(
                        kind=self.kind or res_kind,
                        steps=tuple(self.steps),
                        expected_count=len(self.target_subnames),
                        name="interactive",
                        expression=expr,
                    )
                    self.plans = [Plan(selector=exact_sel, score=(1, 0, 0), explanation="Exact match")]
                elif target_set.issubset(matched_names):
                    extra = len(matched_names) - len(target_set)
                    self.plans = []
                    self.current_result.setText(f"▲ {m_count} {self.kind or res_kind}(s) ({count} target, +{extra} extra){cue} · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #ef6c00; background-color: rgba(239, 108, 0, 0.12); border-radius: 3px; padding: 2px 6px;")
                elif matched_names.issubset(target_set):
                    missing = len(target_set) - len(matched_names)
                    self.plans = []
                    self.current_result.setText(f"▼ {m_count} {self.kind or res_kind}(s) ({count} target, -{missing} missing){cue} · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #c62828; background-color: rgba(198, 40, 40, 0.12); border-radius: 3px; padding: 2px 6px;")
                else:
                    self.plans = []
                    self.current_result.setText(f"⚠ {m_count} {self.kind or res_kind}(s) (mismatched, {count} target){cue} · {t_ms:.1f}ms")
                    self.current_result.setStyleSheet("font-weight: bold; color: #ef6c00; background-color: rgba(239, 108, 0, 0.12); border-radius: 3px; padding: 2px 6px;")
            else:
                self.plans = []
                self.current_result.setText(f"Found {m_count} {res_kind}(s){cue} · {t_ms:.1f}ms (click Set as Target)")
                self.current_result.setStyleSheet("font-weight: bold; color: #0277bd; background-color: rgba(2, 119, 189, 0.12); border-radius: 3px; padding: 2px 6px;")

            # VIS-2: Build rich metric breakdown tooltip for hovering
            if matched_items:
                breakdown = [f"Matched {len(matched_items)} {res_kind}(s):"]
                for it in matched_items[:12]:
                    extra_info = []
                    if getattr(it, "convexity", None):
                        extra_info.append(f"[{it.convexity}]")
                    if hasattr(it, "length") and it.length is not None and math.isfinite(it.length):
                        extra_info.append(f"L={it.length:.2f}mm")
                    if hasattr(it, "radius") and it.radius is not None and math.isfinite(it.radius):
                        extra_info.append(f"R={it.radius:.2f}mm")
                    if hasattr(it, "area") and it.area is not None and math.isfinite(it.area):
                        extra_info.append(f"A={it.area:.2f}mm²")
                    info_str = " · ".join(extra_info)
                    breakdown.append(f"• {it.subname}: {it.geom_type.title()} {info_str}")
                if len(matched_items) > 12:
                    breakdown.append(f"... and {len(matched_items) - 12} more")
                self.current_result.setToolTip("\n".join(breakdown))
            else:
                self.current_result.setToolTip("")

            if self.live_preview_cb.isChecked():
                self._update_3d_preview([item.shape for item in matched_items])
        except Exception as exc:
            self.last_resolved = []
            if hasattr(self, "set_target_btn"):
                self.set_target_btn.setEnabled(False)
            t_ms = (time.perf_counter() - t0) * 1000.0
            self.current_result.setText(f"❌ Syntax/Evaluation: {exc} · {t_ms:.1f}ms")
            self.current_result.setStyleSheet("font-weight: bold; color: #c62828; background-color: rgba(198, 40, 40, 0.12); border-radius: 3px; padding: 2px 6px;")
            self.current_result.setToolTip("")

    def _on_active_clause_changed(self, active_idx: Optional[int]):
        """Focus 3D elements belonging to the active clause under cursor."""
        if not self.live_preview_cb.isChecked() or not self.source_obj:
            return
        self._update_3d_preview(active_clause_idx=active_idx)

    def _toggle_live_preview(self, checked: bool):
        if checked:
            self._update_merged_status()
        else:
            self._clear_synchronized_highlighting()
            self._clear_3d_direction_indicator()
            try:
                if Gui is not None:
                    Gui.Selection.clearSelection()
            except Exception:
                pass

    def _apply_synchronized_highlighting(
        self,
        element_colors: dict[str, tuple[float, float, float, float]],
        missing_names: Optional[set[str]] = None,
    ):
        """Synchronize 3D geometry element colors with selector expression clauses."""
        if not self.source_obj or not hasattr(self.source_obj, "ViewObject"):
            return
        vo = self.source_obj.ViewObject
        if vo is None or not hasattr(vo, "setElementColors"):
            return
        colors = dict(element_colors)
        if missing_names:
            for n in missing_names:
                if n not in colors:
                    colors[n] = (0.91, 0.30, 0.24, 0.0)
        try:
            vo.setElementColors(colors)
        except Exception:
            pass

    def _clear_synchronized_highlighting(self):
        if self.source_obj and hasattr(self.source_obj, "ViewObject"):
            vo = self.source_obj.ViewObject
            if vo and hasattr(vo, "setElementColors"):
                try:
                    vo.setElementColors({})
                except Exception:
                    pass

    # Clean alias
    _clear_3color_highlighting = _clear_synchronized_highlighting

    def _update_3d_direction_indicator(self, expr: str, matched_items: Sequence[Any]):
        """VIS-3: Display a subtle 3D direction indicator on the centroid of matched elements in the viewport."""
        doc = getattr(self.source_obj, "Document", None) or (App.ActiveDocument if App else None)
        if not doc or not expr or not matched_items:
            self._clear_3d_direction_indicator()
            return
        m_dir = re.search(r"([><|#+\-])([XYZ])\b", expr, re.IGNORECASE)
        if not m_dir:
            self._clear_3d_direction_indicator()
            return
        sign, axis = m_dir.group(1), m_dir.group(2).upper()
        axis_vecs = {"X": App.Vector(1, 0, 0), "Y": App.Vector(0, 1, 0), "Z": App.Vector(0, 0, 1)}
        vec = axis_vecs.get(axis, App.Vector(0, 0, 1))
        if sign in ("<", "-"):
            vec = -vec

        centers = [it.center_tuple for it in matched_items if hasattr(it, "center_tuple")]
        if not centers:
            return
        cx = sum(c[0] for c in centers) / len(centers)
        cy = sum(c[1] for c in centers) / len(centers)
        cz = sum(c[2] for c in centers) / len(centers)
        p0 = App.Vector(cx, cy, cz)
        p1 = p0 + vec * 15.0

        try:
            import Part
            indicator_name = "_FS_DirectionIndicator"
            ind = doc.getObject(indicator_name)
            if ind is None:
                ind = doc.addObject("Part::Feature", indicator_name)
            ind.Shape = Part.makeLine(p0, p1)
            if hasattr(ind, "ViewObject") and ind.ViewObject:
                ind.ViewObject.LineColor = (1.0, 0.55, 0.0)
                ind.ViewObject.LineWidth = 3.5
                ind.ViewObject.PointSize = 5.0
                ind.ViewObject.Visibility = True
        except Exception:
            pass

    def _clear_3d_direction_indicator(self):
        doc = getattr(self.source_obj, "Document", None) or (App.ActiveDocument if App else None)
        if doc:
            ind = doc.getObject("_FS_DirectionIndicator")
            if ind is not None:
                try:
                    doc.removeObject("_FS_DirectionIndicator")
                except Exception:
                    pass

    def _update_3d_preview(self, shapes: Optional[list[Any]] = None, active_clause_idx: Optional[int] = None):
        if not self.live_preview_cb.isChecked() or not self.source_obj:
            return
        if getattr(self, "_preview_updating", False):
            return
        self._preview_updating = True
        try:
            expr = self.expr_edit.toPlainText().strip()
            if expr:
                tol = getattr(self, "tolerance", 1e-4)
                if active_clause_idx is None and hasattr(self.expr_edit, "highlighter"):
                    active_clause_idx = getattr(self.expr_edit.highlighter, "_active_clause_idx", None)
                clauses, element_colors = compute_synchronized_colors(
                    self.source_obj, expr, kind=self.kind, tolerance=tol, active_clause_idx=active_clause_idx
                )
                if hasattr(self.expr_edit, "highlighter"):
                    self.expr_edit.highlighter.set_synchronized_clauses(clauses, active_clause_idx)

                refs = evaluate_expression(self.source_obj, expr, kind=self.kind, tolerance=tol)
                if refs:
                    add_selection(refs, clear=True)
                matched_names = {r.subname for r in refs}
                intended_names = set(self.captured_selection or self.target_subnames or [])
                missing_names = intended_names - matched_names
                self._apply_synchronized_highlighting(element_colors, missing_names)
                self._update_3d_direction_indicator(expr, getattr(self, "last_resolved", []))
            else:
                self._clear_synchronized_highlighting()
        except Exception:
            pass
        finally:
            self._preview_updating = False

    def _exact_selector(self) -> Optional[Selector]:
        if not self.source_obj or not self.kind:
            return None
        expr = self.expr_edit.toPlainText().strip()
        if not expr:
            return None
        try:
            tol = getattr(self, "tolerance", 1e-4)
            refs = evaluate_expression(self.source_obj, expr, kind=self.kind, tolerance=tol)
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
            if not expr and self.source_obj and self.target_subnames:
                ins, _ = autocomplete_at_cursor(self.source_obj, self.target_subnames, self.kind or "Shape", "", 0)
                if ins:
                    expr = ins
                    self.expr_edit.setPlainText(expr)
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
        if hasattr(self, "status") and self.status is not None:
            self.status.setText("Python snippet copied to clipboard!")
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
            "<li><b>Tags:</b> <code>:concave</code>, <code>:convex</code>, <code>:smooth</code>, <code>:closed</code>, <code>:planar</code></li>"
            "<li><b>Clustering:</b> <code>&gt;&gt;radius[0]</code> (largest), <code>&lt;&lt;radius[0]</code> (smallest), <code>&gt;&gt;length[0:2]</code></li>"
            "<li><b>Comparison:</b> <code>radius == 10.0</code>, <code>3.0 &lt;= diameter &lt;= 8.0</code></li>"
            "<li><b>Relational:</b> <code>adjacent_to('Face1')</code>, <code>coplanar_to(...)</code></li>"
            "<li><b>Descending:</b> <code>faces('&gt;Z').edges('&lt;X')</code></li>"
            "<li><b>⚡ Autocomplete:</b> Click button or press <b>Tab</b> to autocomplete!</li>"
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
