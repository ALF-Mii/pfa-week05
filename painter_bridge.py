"""Substance 3D Painter bridge - a live port your agent can drive.

Substance 3D Painter cannot be scripted from outside the application, and it has
no official MCP server. Its Python API (`substance_painter`) only runs *inside*
the app. So this plugin turns Painter into a small command server:

  * When the plugin starts it opens a TCP port on localhost (default 7301).
  * A tiny terminal client (`painter_client.py`) connects to that port, sends
    Python, and the code runs live inside Painter. Anything the code prints, and
    its result, come back to the terminal.
  * It is the same idea as Maya's command port: you watch the app react while
    the script talks to it.

The plugin also adds the usual dockable panel + toolbar button, which shows the
bridge status and mirrors, in the app itself, every command that arrives and all
output it produces (handy while recording).

Install:
  1. Copy this file into Painter's Python plugin folder, for example:
     .../Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
  2. Start Substance 3D Painter.
  3. Open the "Python" menu and enable "painter_bridge".
  4. The "Painter Bridge" panel appears and the port starts listening.

Use it from the terminal (with Painter's own Python, which is always present):

  python painter_client.py --action ping
  python painter_client.py -c "print(substance_painter.project.name())"
  python painter_client.py --action stylize --type Toon --style Cracks

Painter 10.1+ uses Qt6 (PySide6) while older versions use Qt5 (PySide2). This
file picks the right binding automatically.
"""
import ast
import io
import json
import os
import socket
import sys
import threading
import traceback

try:
    import queue  # Python 3
except ImportError:  # pragma: no cover - Python 2 safety net
    import Queue as queue

import substance_painter.application
import substance_painter.js
import substance_painter.project
import substance_painter.resource
import substance_painter.ui

# --- Qt5 / Qt6 compatibility ------------------------------------------------
_IS_QT5 = substance_painter.application.version_info() < (10, 1, 0)

if _IS_QT5:
    from PySide2 import QtCore
    from PySide2 import QtGui
    from PySide2 import QtWidgets
else:
    from PySide6 import QtCore
    from PySide6 import QtGui
    from PySide6 import QtWidgets

PANEL_NAME = "PainterBridgePanel"  # Unique name so Painter remembers the dock.
_END = QtGui.QTextCursor.MoveOperation.End  # Cursor position: end of document.

# Where the bridge listens. Override with the PAINTER_BRIDGE_PORT env var if you
# already use 7301 for something else. The client reads the same variable.
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("PAINTER_BRIDGE_PORT", "7301"))
BRIDGE_MAX_MESSAGE = 1 << 20  # 1 MiB ceiling on a single request.
BRIDGE_TIMEOUT = 120.0  # How long the client waits for the app to answer.

# Module level handles (kept so we can clean everything up on unload).
_output_box = None  # Text box that mirrors print() output and bridge traffic.
_status_label = None  # Small label showing whether the port is listening.
_original_stdout = None  # Saved so print() can be restored on unload.
_panel = None  # The widget we dock.
_dock = None  # The QDockWidget Painter wraps it in (used to re-show it).
_toolbar_button = None  # The one-click button on the plugins toolbar.
_menu_action = None  # The Window-menu entry.

_server = None  # The BridgeServer thread.
_poll_timer = None  # Main-thread QTimer that drains the request queue.
_requests = queue.Queue()  # Requests waiting to run on the main thread.


# ==========================================================================
# Actions: named things the bridge can be asked to do.
# ==========================================================================
BRIDGE_ACTIONS = {}


def bridge_action(name):
    """Register a function so the client can call it with --action NAME."""
    def decorator(function):
        BRIDGE_ACTIONS[name] = function
        return function
    return decorator


@bridge_action("ping")
def _action_ping():
    """Trivial liveness check."""
    return "pong"


@bridge_action("project_info")
def _action_project_info():
    """Return a short description of the open project."""
    if not substance_painter.project.is_open():
        return "No project is open."
    return "Project: %s | mesh: %s" % (
        substance_painter.project.name(),
        substance_painter.project.last_imported_mesh_path())


def _guess_sample_mesh():
    """Find a small mesh we can use to create a demo project.

    Honours the PAINTER_SAMPLE_MESH env var first, then falls back to the sample
    meshes that ship with Painter's automated-test resources.
    """
    candidates = []
    environment = os.environ.get("PAINTER_SAMPLE_MESH")
    if environment:
        candidates.append(environment)
    try:
        modules_dir = os.path.dirname(os.path.dirname(substance_painter.__file__))
        for name in ("4udims.obj", "4cubes3udims.fbx"):
            candidates.append(os.path.join(modules_dir, "automated_tests",
                                           "resources", name))
    except Exception:
        pass
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


@bridge_action("create_project")
def _action_create_project(mesh=None):
    """Create a demo project from a mesh (default: a bundled sample)."""
    if substance_painter.project.is_open():
        return "A project is already open - close it first to create a new one."
    if not mesh:
        mesh = _guess_sample_mesh()
    if not mesh or not os.path.isfile(mesh):
        raise FileNotFoundError(
            "No mesh found. Pass one, e.g. mesh=r'C:\\path\\to\\model.fbx'.")
    substance_painter.project.create(mesh_file_path=mesh)
    return "Created a project from %s" % mesh


@bridge_action("stylize")
def _action_stylize(shader_type="Toon", style="Cracks", **params):
    """Apply the stylized shader (from stylized_normals) to the open model.

    `shader_type` and `style` are names from that plugin's combos; any extra
    keyword parameters (style_scale=..., saturation=...) are passed straight
    to the shader.
    """
    import stylized_normals as sn

    if not substance_painter.project.is_open():
        raise RuntimeError("No project is open. Open one, or run "
                           "--action create_project first.")

    types = dict(sn.SHADER_TYPES)
    styles = dict(sn.STYLE_OPTIONS)
    if shader_type not in types:
        raise ValueError("Unknown shader type %r. Try one of: %s"
                         % (shader_type, ", ".join(types)))
    if style not in styles:
        raise ValueError("Unknown style %r. Try one of: %s"
                         % (style, ", ".join(styles)))

    # Install the shader once (skip if it is already in the shelf).
    if sn._find_shader_resource() is None:
        sn._install_shader()
    if sn._find_shader_resource() is None:
        raise RuntimeError("Could not install '%s' into the shelf." % sn.SHADER_NAME)

    sn._apply_shader()

    values = {"shader_type": types[shader_type], "style": styles[style]}
    values.update(params)
    sn._set_parameters(values)
    return "Applied shader type '%s' with style '%s'." % (shader_type, style)


@bridge_action("reset_shading")
def _action_reset_shading():
    """Return the viewport to Painter's default (normal) shader."""
    import stylized_normals as sn
    if not substance_painter.project.is_open():
        raise RuntimeError("No project is open.")
    sn._reset_shading()
    return "Reset to normal shading."


# ==========================================================================
# The server side of the bridge (runs in a background thread).
# ==========================================================================
class _BridgeRequest(object):
    """One client request plus the slot its answer is written back to."""

    def __init__(self, message):
        self.message = message or {}
        self.response = None
        self.event = threading.Event()

    @property
    def code(self):
        return self.message.get("code")

    @property
    def action(self):
        return self.message.get("action")

    @property
    def args(self):
        return self.message.get("args") or {}


class BridgeServer(threading.Thread):
    """Accept connections and hand each request to the main thread.

    A background thread only does the network I/O and queues the work; the
    actual Painter calls happen on the main thread (see _run_request), because
    the Qt and substance_painter APIs must be used there.
    """

    def __init__(self, host, port, request_queue):
        super(BridgeServer, self).__init__(name="PainterBridgeServer")
        self.daemon = True
        self._queue = request_queue
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self.host, self.port = self._sock.getsockname()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break  # Socket closed by stop().
            worker = threading.Thread(target=self._serve, args=(conn,))
            worker.daemon = True
            worker.start()

    def _serve(self, conn):
        with conn:
            try:
                message = self._read_message(conn)
            except Exception as error:
                self._send(conn, {"ok": False, "error": "bad request: %s" % error})
                return
            request = _BridgeRequest(message)
            self._queue.put(request)
            if not request.event.wait(BRIDGE_TIMEOUT):
                request.response = {"ok": False,
                                    "error": "the app did not answer in time"}
            self._send(conn, request.response or {"ok": False, "error": "no reply"})

    @staticmethod
    def _read_message(conn):
        data = b""
        while b"\n" not in data and len(data) < BRIDGE_MAX_MESSAGE:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        line = data.split(b"\n", 1)[0]
        if not line:
            raise ValueError("empty request")
        return json.loads(line.decode("utf-8"))

    @staticmethod
    def _send(conn, payload):
        try:
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            pass

    def stop(self):
        """Close the socket so the accept loop ends, then wait for it."""
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


# ==========================================================================
# The app side: run requests on the main thread.
# ==========================================================================
class _CaptureRedirect(object):
    """Capture print() output AND mirror it into the panel's output box."""

    def __init__(self, buffer, mirror):
        self._buffer = buffer
        self._mirror = mirror

    def write(self, text):
        self._buffer.write(text)
        if text and self._mirror is not None:
            self._mirror.moveCursor(_END)
            self._mirror.insertPlainText(text)
            self._mirror.moveCursor(_END)

    def flush(self):
        pass


def _make_namespace():
    """Globals for bridge code: the scripting modules, ready to use."""
    return {
        "__name__": "__painter_bridge__",
        "substance_painter": substance_painter,
        "application": substance_painter.application,
        "project": substance_painter.project,
        "js": substance_painter.js,
        "resource": substance_painter.resource,
        "actions": BRIDGE_ACTIONS,
    }


def _execute(request, namespace):
    """Run one request in `namespace`, returning its result (or None)."""
    if request.action:
        function = BRIDGE_ACTIONS.get(request.action)
        if function is None:
            raise KeyError("unknown action %r (known: %s)"
                           % (request.action, ", ".join(sorted(BRIDGE_ACTIONS))))
        return function(**request.args)

    code = request.code or ""
    tree = ast.parse(code, "<bridge>", "exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        # exec everything except the final expression, then evaluate it and
        # hand the value back to the client (like a REPL would).
        prefix = ast.Module(body=tree.body[:-1], type_ignores=[])
        if prefix.body:
            exec(compile(prefix, "<bridge>", "exec"), namespace)
        final = ast.Expression(tree.body[-1].value)
        return eval(compile(final, "<bridge>", "eval"), namespace)
    exec(compile(tree, "<bridge>", "exec"), namespace)
    return None


def _run_request(request):
    """Run a queued request on the main thread and store its answer."""
    namespace = _bridge_namespace
    buffer = io.StringIO()
    previous_out, previous_err = sys.stdout, sys.stderr
    sys.stdout = _CaptureRedirect(buffer, _output_box)
    sys.stderr = _CaptureRedirect(buffer, _output_box)
    try:
        result = _execute(request, namespace)
        response = {"ok": True, "stdout": buffer.getvalue(),
                    "result": None if result is None else repr(result)}
    except Exception:
        response = {"ok": False, "stdout": buffer.getvalue(),
                    "error": traceback.format_exc()}
    finally:
        sys.stdout, sys.stderr = previous_out, previous_err
        request.response = response
        request.event.set()


def _poll_bridge():
    """Drain the request queue (called by a QTimer on the main thread)."""
    while True:
        try:
            request = _requests.get_nowait()
        except queue.Empty:
            return
        _run_request(request)


# ==========================================================================
# Panel / UI wiring.
# ==========================================================================
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
    global _output_box, _status_label

    panel = QtWidgets.QWidget()
    panel.setObjectName(PANEL_NAME)  # Lets Painter save/restore dock geometry.
    panel.setWindowTitle("Painter Bridge")
    # A window icon makes Painter add its own "reopen dock" quick button.
    panel.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
        QtWidgets.QStyle.SP_FileDialogDetailedView))

    layout = QtWidgets.QVBoxLayout(panel)
    layout.setSpacing(6)

    info = QtWidgets.QLabel(
        "Live bridge on %s:%d.\n"
        "Send commands from a terminal with painter_client.py; they run here "
        "and their output appears below." % (BRIDGE_HOST, BRIDGE_PORT))
    info.setWordWrap(True)
    layout.addWidget(info)

    _status_label = QtWidgets.QLabel("Status: starting...")
    _status_label.setWordWrap(True)
    layout.addWidget(_status_label)

    log_button = QtWidgets.QPushButton("Log project info")
    log_button.clicked.connect(_log_project_info)
    layout.addWidget(log_button)

    _output_box = QtWidgets.QPlainTextEdit()
    _output_box.setReadOnly(True)
    _output_box.setMinimumHeight(200)
    layout.addWidget(_output_box)

    clear_button = QtWidgets.QPushButton("Clear output")
    clear_button.clicked.connect(_clear_output)
    layout.addWidget(clear_button)

    return panel


def _log_project_info(*_):
    """Print a quick summary of the current project (read-only, safe)."""
    print("Project open:", substance_painter.project.is_open())
    if not substance_painter.project.is_open():
        print("Nothing else to read until a project is open.")
        return
    print("Name:", substance_painter.project.name())
    print("File:", substance_painter.project.file_path())
    print("Mesh:", substance_painter.project.last_imported_mesh_path())


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


# ==========================================================================
# Plugin lifecycle.
# ==========================================================================
_bridge_namespace = _make_namespace()


def _start_bridge():
    """Open the port and start draining requests on the main thread."""
    global _server, _poll_timer

    try:
        _server = BridgeServer(BRIDGE_HOST, BRIDGE_PORT, _requests)
        _server.start()
    except OSError as error:
        _server = None
        if _status_label is not None:
            _status_label.setText("Status: could not open port %d (%s)."
                                  % (BRIDGE_PORT, error))
        print("Bridge could not open %s:%d - %s" % (BRIDGE_HOST, BRIDGE_PORT, error))
        return

    _poll_timer = QtCore.QTimer()
    _poll_timer.setInterval(40)  # ~25 checks per second.
    _poll_timer.timeout.connect(_poll_bridge)
    _poll_timer.start()

    if _status_label is not None:
        _status_label.setText("Status: listening on %s:%d"
                              % (_server.host, _server.port))
    print("Bridge listening on %s:%d" % (_server.host, _server.port))


def _stop_bridge():
    """Close the port and stop the poll timer."""
    global _server, _poll_timer

    if _poll_timer is not None:
        try:
            _poll_timer.stop()
        except Exception:
            pass
        _poll_timer = None
    if _server is not None:
        _server.stop()
        _server = None


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

    _start_bridge()
    print("Painter bridge ready.")


def close_plugin():
    """Called by Painter when the plugin is disabled."""
    global _original_stdout, _panel, _dock, _toolbar_button, _menu_action

    _stop_bridge()

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
