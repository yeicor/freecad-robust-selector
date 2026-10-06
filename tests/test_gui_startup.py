import FreeCAD as App
import FreeCADGui as Gui

def test_inject_partdesign():
    Gui.showMainWindow()
    try:
        from fs_commands import register_commands
        register_commands()
        pd = Gui.getWorkbench("PartDesignWorkbench")
        pd.appendToolbar("Feature Selection", ["FeatureSelector_Robustify"])
        print("INJECTION_SUCCESSFUL")
    except Exception as e:
        print(f"INJECTION_FAILED: {e}")
