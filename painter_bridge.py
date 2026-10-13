"""Substance 3D Painter bridge panel for developing scripts inside Painter.

This is a small plugin that adds a dockable panel to Substance 3D Painter. The
panel is a sandbox for iterating on scripting API code:

  * "Log project info" runs a few safe, read-only API calls.
  * "Run my script" calls `run_my_script()` below, which is the part you are
    meant to edit while working on your tool.
  * Anything written with `print()` shows up in the panel's output box.

Showing the panel is one click:
  * A "Painter Bridge" button is added to the plugins toolbar.
  * A "Show Painter Bridge panel" entry is added to the Window menu.
  * Because the panel has a window icon, Painter also adds its own quick
    reopen button if you close the dock.

Install:
  1. Copy this file into Painter's Python plugin folder, for example:
     .../Documents/Adobe/Substance 3D Painter/python/plugins/
  2. Start Substance 3D Painter.
  3. Open the "Python" menu and enable "painter_bridge".
  4. The "Painter Bridge" panel appears; drag it wherever you like.

Painter 10.1+ uses Qt6 (PySide6) while older versions use Qt5 (PySide2). This
file picks the right binding automatically.
"""
import sys

import substance_painter.application
import substance_painter.project
import substance_painter.ui

# --- Qt5 / Qt6 compatibility ------------------------------------------------
_IS_QT5 = substance_painter.application.version_info() < (10, 1, 0)

if _IS_QT5:
    from PySide2 import QtGui
    from PySide2 import QtWidgets
else:
    from PySide6 import QtGui
    from PySide6 import QtWidgets

PANEL_NAME = "PainterBridgePanel"  # Unique name so Painter remembers the dock.
_END = QtGui.QTextCursor.MoveOperation.End  # Cursor position: end of document.

_output_box = None  # Text box that mirrors print() output.
_original_stdout = None  # Saved so print() can be restored on unload.
_panel = None  # The widget we dock.
_dock = None  # The QDockWidget Painter wraps it in (used to re-show it).
_toolbar_button = None  # The one-click button on the plugins toolbar.
_menu_action = None  # The Window-menu entry.


class _OutputRedirect(object):
    """Mirror text written with print() into the panel and the real console."""

    def __init__(self, text_edit, original):
        self._text_edit = text_edit
        self._original = original

    def write(self, text):
        if self._original is not None:
            self._original.write(text)
        if text and self._text_edit is not None:
            self._text_edit.moveCursor(_END)
            self._text_edit.insertPlainText(text)
            self._text_edit.moveCursor(_END)

    def flush(self):
        if self._original is not None:
            self._original.flush()


def run_my_script(*_):
    """Put the script you are developing here. print() shows in the panel.

    The functions in `substance_painter` are only available inside Painter,
    so use this panel to try them out. Below are a couple of examples; replace
    them with your own code.
    """
    print("Hello from the Painter bridge!")

    if substance_painter.project.is_open():
        print("Project: %s" % substance_painter.project.name())
    else:
        print("No project is open yet - create or open one in Painter.")


def _log_project_info(*_):
    """Print a quick summary of the current project (read-only, safe)."""
    print("Project open:", substance_painter.project.is_open())
    if not substance_painter.project.is_open():
        print("Nothing else to read until a project is open.")
        return
    print("Name:", substance_painter.project.name())
    print("File:", substance_painter.project.file_path())
    print("Mesh:", substance_painter.project.last_imported_mesh_path())


def _clear_output(*_):
    """Empty the output box."""
    if _output_box is not None:
        _output_box.clear()


def _show_panel(*_):
    """Bring the bridge panel back into view (used by the button and menu)."""
    if _dock is not None:
        _dock.show()
        _dock.raise_()
    elif _panel is not None:
        _panel.show()


def _build_panel():
    """Create the panel widget and return it."""
    global _output_box

    panel = QtWidgets.QWidget()
    panel.setObjectName(PANEL_NAME)  # Lets Painter save/restore dock geometry.
    panel.setWindowTitle("Painter Bridge")
    # A window icon makes Painter add its own "reopen dock" quick button.
    panel.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
        QtWidgets.QStyle.SP_FileDialogDetailedView))

    layout = QtWidgets.QVBoxLayout(panel)
    layout.setSpacing(6)

    info = QtWidgets.QLabel(
        "Sandbox for Substance 3D Painter scripting.\n"
        "Edit run_my_script() in painter_bridge.py, then click Run.")
    info.setWordWrap(True)
    layout.addWidget(info)

    log_button = QtWidgets.QPushButton("Log project info")
    log_button.clicked.connect(_log_project_info)
    layout.addWidget(log_button)

    run_button = QtWidgets.QPushButton("Run my script")
    run_button.clicked.connect(run_my_script)
    layout.addWidget(run_button)

    _output_box = QtWidgets.QPlainTextEdit()
    _output_box.setReadOnly(True)
    _output_box.setMinimumHeight(160)
    layout.addWidget(_output_box)

    clear_button = QtWidgets.QPushButton("Clear output")
    clear_button.clicked.connect(_clear_output)
    layout.addWidget(clear_button)

    return panel


def _add_toolbar_button():
    """Add a one-click 'Painter Bridge' button to the plugins toolbar."""
    global _toolbar_button

    button = QtWidgets.QToolButton()
    button.setText("Painter Bridge")
    button.setToolTip("Show the Painter Bridge panel")
    button.clicked.connect(_show_panel)
    try:
        substance_painter.ui.add_plugins_toolbar_widget(button)
        _toolbar_button = button
    except Exception:
        button.deleteLater()  # Older builds without the plugins toolbar.


def _add_menu_action():
    """Add a 'Show Painter Bridge panel' entry to the Window menu."""
    global _menu_action

    action = QtWidgets.QAction("Show Painter Bridge panel")
    action.triggered.connect(_show_panel)
    try:
        substance_painter.ui.add_action(
            substance_painter.ui.ApplicationMenu.Window, action)
        _menu_action = action
    except Exception:
        pass  # Not fatal: the toolbar button still works.


def start_plugin():
    """Called by Painter when the plugin is enabled."""
    global _original_stdout, _panel, _dock

    _panel = _build_panel()
    _dock = substance_painter.ui.add_dock_widget(_panel)
    _add_toolbar_button()
    _add_menu_action()

    # Route print() output into the panel so scripts are easy to debug.
    _original_stdout = sys.stdout
    sys.stdout = _OutputRedirect(_output_box, _original_stdout)
    sys.stderr = _OutputRedirect(_output_box, sys.__stderr__)

    print("Painter bridge ready.")


def close_plugin():
    """Called by Painter when the plugin is disabled."""
    global _original_stdout, _panel, _dock, _toolbar_button, _menu_action

    if _original_stdout is not None:
        sys.stdout = _original_stdout
        sys.stderr = sys.__stderr__
        _original_stdout = None

    for element in (_toolbar_button, _menu_action, _panel):
        if element is not None:
            try:
                substance_painter.ui.delete_ui_element(element)
            except Exception:
                pass

    _toolbar_button = None
    _menu_action = None
    _panel = None
    _dock = None


if __name__ == "__main__":
    start_plugin()
