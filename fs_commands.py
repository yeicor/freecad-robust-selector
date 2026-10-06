"""Public FreeCAD GUI commands for the Feature Selector workbench.

The commands deliberately expose the explicit workflow. There is no automatic
capture, document observer, timer, selection interception, or hidden binding.
"""
from __future__ import annotations

import os
import FreeCAD as App
import FreeCADGui as Gui

_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
if os.path.isdir(_ICONS_DIR) and Gui is not None and hasattr(Gui, "addIconPath"):
    Gui.addIconPath(_ICONS_DIR)


def _panel():
    from fs_gui import FeatureSelectorPanel
    return FeatureSelectorPanel.instance()


class RobustifySelectionCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelector.svg",
            "MenuText": "Robust Selection…",
            "ToolTip": "Capture the current FreeCAD selection and build an explicit, editable semantic selector.",
            "Accel": "S, R",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        panel = _panel()
        panel.show_panel()
        panel.learn()


class OpenPanelCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelector.svg",
            "MenuText": "Open Feature Selector Panel",
            "ToolTip": "Open the explicit semantic-selector editor without capturing a selection.",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        _panel().show_panel()


class EditSelectedSelectorCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelector.svg",
            "MenuText": "Edit Selected Robust Selector",
            "ToolTip": "Open the selected persistent robust selector in the semantic editor.",
        }

    def IsActive(self):
        if App.ActiveDocument is None:
            return False
        for obj in Gui.Selection.getSelection():
            if hasattr(obj, "Query") and hasattr(obj, "BaseObject"):
                return True
        return False

    def Activated(self):
        for obj in Gui.Selection.getSelection():
            if hasattr(obj, "Query") and hasattr(obj, "BaseObject"):
                _panel().open_editor_for_object(obj)
                return


class ApplySelectorCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelectorApply.svg",
            "MenuText": "Preview Robust Selection",
            "ToolTip": "Resolve the active selector and place its result into FreeCAD's normal global selection.",
        }

    def IsActive(self):
        return _panel().has_active_selector()

    def Activated(self):
        _panel().preview_selection()


class CreateSelectorObjectCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelector.svg",
            "MenuText": "Create / Save Robust Selector",
            "ToolTip": "Persist the active semantic selector as an explicit FeaturePython document object.",
        }

    def IsActive(self):
        return _panel().has_active_selector()

    def Activated(self):
        _panel().save_selector()



class RobustifyFeatureCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelectorFeature.svg",
            "MenuText": "Robustify Selected Feature",
            "ToolTip": "1-click convert the selected native feature (Fillet, Chamfer, Sketch, Binder) to use an explicit robust semantic selector.",
            "Accel": "S, F",
        }

    def IsActive(self):
        if App.ActiveDocument is None:
            return False
        from fs_bindings import _profile_sketches, inspect_feature_references

        def _fragile(infos):
            return any(
                i.get("subnames") and i.get("subnames") != [""] and i.get("kind") != "Shape"
                for i in infos
            )

        for obj in Gui.Selection.getSelection():
            try:
                if _fragile(inspect_feature_references(obj)):
                    return True
                for sketch in _profile_sketches(obj):
                    if _fragile(inspect_feature_references(sketch)):
                        return True
            except (AttributeError, TypeError, ValueError):
                continue
        return False

    def Activated(self):
        from fs_bindings import (
            apply_robustify_plan,
            compute_robustify_plan,
            inspect_feature_references,
            prepare_robustify_feature,
        )
        from fs_tasks import freeze_recomputes, submit as submit_background, unfreeze_recomputes

        selection = Gui.Selection.getSelection()
        for obj in selection:
            if not inspect_feature_references(obj):
                continue
            try:
                prepared = prepare_robustify_feature(obj)
            except Exception as exc:
                if hasattr(App, "Console"):
                    App.Console.PrintError(f"FeatureSelector: Could not robustify {obj.Label}: {exc}\n")
                return
            if "existing" in prepared:
                _panel().open_editor_for_object(prepared["existing"])
                return
            bundle = prepared["prepared"]
            doc = bundle["doc"]
            freeze_recomputes(doc)

            def _done(best_plan, _bundle=bundle, _doc=doc, _obj=obj):
                unfreeze_recomputes(_doc)
                try:
                    selector_obj, _plan = apply_robustify_plan(_doc, _bundle, best_plan)
                except Exception as exc:
                    if hasattr(App, "Console"):
                        App.Console.PrintError(f"FeatureSelector: Could not robustify {_obj.Label}: {exc}\n")
                    return
                _panel().open_editor_for_object(selector_obj)
                if hasattr(App, "Console"):
                    App.Console.PrintMessage(
                        f"FeatureSelector: Successfully robustified {_obj.Label} -> created {selector_obj.Label}\n"
                    )

            def _failed(message, _doc=doc, _obj=obj):
                unfreeze_recomputes(_doc)
                if hasattr(App, "Console"):
                    App.Console.PrintError(f"FeatureSelector: Could not robustify {_obj.Label}: {message}\n")

            submit_background(
                lambda: compute_robustify_plan(bundle),
                on_done=_done,
                on_failed=_failed,
            )
            return


class RobustifyAllFeaturesCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelectorAll.svg",
            "MenuText": "Robustify Entire Document",
            "ToolTip": "Scan the entire document and 1-click convert all native features referencing subelements to robust semantic selectors.",
            "Accel": "S, A",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        from fs_bindings import format_robustify_all_report, robustify_all_features
        summary = robustify_all_features(App.ActiveDocument)
        report = format_robustify_all_report(summary)
        if hasattr(App, "Console"):
            App.Console.PrintMessage(report + "\n")
        panel = _panel()
        form = getattr(panel, "form", None) or getattr(panel, "dock", None)
        if form is not None:
            try:
                visible = form.isVisible() if hasattr(form, "isVisible") else True
            except (AttributeError, RuntimeError):
                visible = False
            if visible:
                panel.status.setText(
                    f"Robustified {summary['robustified_count']} features in {summary['time_ms']:.1f}ms "
                    f"({summary['skipped_count']} skipped, {summary['failed_count']} failed)."
                )


class AuditSelectionsCommand:
    def GetResources(self):
        return {
            "Pixmap": "FeatureSelector.svg",
            "MenuText": "Audit Robust Selections",
            "ToolTip": "Inspect all robust selectors and bindings in the active document and print a detailed health report.",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        from fs_bindings import (
            assemble_audit_summary,
            collect_audit_snapshots,
            evaluate_audit_snapshots,
            format_audit_report,
        )
        from fs_tasks import freeze_recomputes, submit as submit_background, unfreeze_recomputes

        doc = App.ActiveDocument
        try:
            doc_name, items = collect_audit_snapshots(doc)
        except Exception as exc:
            if hasattr(App, "Console"):
                App.Console.PrintError(f"FeatureSelector: Audit failed: {exc}\n")
            return
        freeze_recomputes(doc)

        def _done(rows, _doc=doc, _name=doc_name):
            unfreeze_recomputes(_doc)
            audit = assemble_audit_summary(_name, rows)
            report = format_audit_report(audit)
            if hasattr(App, "Console"):
                App.Console.PrintMessage(report + "\n")

        def _failed(message, _doc=doc):
            unfreeze_recomputes(_doc)
            if hasattr(App, "Console"):
                App.Console.PrintError(f"FeatureSelector: Audit failed: {message}\n")

        # Fire-and-forget: the slots report when done, the UI never blocks.
        submit_background(
            lambda: evaluate_audit_snapshots(items),
            on_done=_done,
            on_failed=_failed,
        )


COMMANDS = [
    ("FeatureSelector_Robustify", RobustifySelectionCommand()),
    ("FeatureSelector_RobustifyFeature", RobustifyFeatureCommand()),
    ("FeatureSelector_RobustifyAll", RobustifyAllFeaturesCommand()),
    ("FeatureSelector_Open", OpenPanelCommand()),
    ("FeatureSelector_EditSelected", EditSelectedSelectorCommand()),
    ("FeatureSelector_Apply", ApplySelectorCommand()),
    ("FeatureSelector_CreateObject", CreateSelectorObjectCommand()),
    ("FeatureSelector_Audit", AuditSelectionsCommand()),
]

TOOLBAR_COMMANDS = [
    "FeatureSelector_Robustify",
    "FeatureSelector_RobustifyFeature",
    "FeatureSelector_RobustifyAll",
]

MENU_COMMANDS = [
    "FeatureSelector_Robustify",
    "FeatureSelector_RobustifyFeature",
    "FeatureSelector_RobustifyAll",
    "FeatureSelector_Open",
    "FeatureSelector_EditSelected",
    "FeatureSelector_Apply",
    "FeatureSelector_CreateObject",
    "FeatureSelector_Audit",
]

CONTEXT_COMMANDS = [
    "FeatureSelector_Robustify",
    "FeatureSelector_RobustifyFeature",
    "FeatureSelector_RobustifyAll",
    "FeatureSelector_EditSelected",
]


def register_commands():
    names = []
    for name, command in COMMANDS:
        try:
            Gui.addCommand(name, command)
        except RuntimeError:
            # Re-loading a workbench in an existing FreeCAD session tries to
            # re-register already registered command names.
            pass
        names.append(name)
    return names
