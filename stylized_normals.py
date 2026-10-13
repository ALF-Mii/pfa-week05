"""Stylized normal-map generator for Substance 3D Painter.

A single-file plugin that adds a "Stylized Normals" panel to Substance 3D
Painter. It generates tileable, hand-painted looking normal maps and applies
them straight to the Normal *mesh map* of the active Texture Set - no need to
drag the texture onto a layer yourself.

Three ways to make the height field that drives the normals:

  * Procedural pattern - a sum of tileable sine waves (Painterly / Waves /
    Chunky / Cracks), with control over scale, octaves, strength and seed.
  * Base material - reads the Texture Set's composited base color (via the
    scripting API's map export) and turns its luminance into a height field, so
    the stylized bumps follow whatever you have already painted.
  * Base material + pattern detail - blends the two, so the pattern adds detail
    on top of the painted material.

The generated map is written to your shelf (so it persists and shows up in the
Assets window), imported into the project, and assigned as the Normal mesh map
of the active Texture Set.

Install:
  1. Copy this file into Painter's Python plugin folder, for example:
     .../Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
  2. Start Substance 3D Painter.
  3. Open the "Python" menu and enable "stylized_normals".
  4. The "Stylized Normals" panel appears (also a toolbar button and a
     Window > "Show Stylized Normals panel" entry).

Painter 10.1+ uses Qt6 (PySide6) while older versions use Qt5 (PySide2). This
file picks the right binding automatically.
"""
import json
import math
import os
import random
import struct
import sys
import tempfile
import zlib

import substance_painter.application
import substance_painter.js
import substance_painter.project
import substance_painter.resource
import substance_painter.textureset
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

PANEL_NAME = "StylizedNormalsPanel"  # Unique name so Painter remembers the dock.
_END = QtGui.QTextCursor.MoveOperation.End  # Cursor position: end of document.

# Module level handles (kept so we can clean everything up on unload).
_output_box = None  # Text box that mirrors print() output.
_original_stdout = None  # Saved so print() can be restored on unload.
_panel = None  # The widget we dock.
_dock = None  # The QDockWidget Painter wraps it in (used to re-show it).
_toolbar_button = None  # The one-click button on the plugins toolbar.
_menu_action = None  # The Window-menu entry.
_source_combo = None  # Height source: procedural / base material / hybrid.
_pattern_combo = None  # Procedural pattern style.
_size_combo = None  # Output size.
_strength_spin = None  # Bump strength.
_scale_spin = None  # Pattern frequency multiplier.
_octaves_spin = None  # Number of fractal detail levels.
_detail_spin = None  # How much pattern detail to mix into the base material.
_contrast_spin = None  # Height contrast (percent).
_seed_spin = None  # Random seed.
_invert_check = None  # Invert the height field.

TWO_PI = 2.0 * math.pi

# Base integer frequencies for each procedural style. Integer frequencies keep
# the pattern seamless / tileable across the UV island. "ridged" styles turn
# the sine into sharp creases (used by the "Cracks" look).
STYLE_DEFINITIONS = {
    "painterly": {
        "label": "Painterly strokes",
        "waves": [(1, 3, 0.9), (2, 5, 0.6), (1, 8, 0.4)],
        "ridged": False,
        "quantize": 0.0,
    },
    "waves": {
        "label": "Waves (soft bumps)",
        "waves": [(1, 1, 0.8), (2, -1, 0.6), (1, 2, 0.5)],
        "ridged": False,
        "quantize": 0.0,
    },
    "chunky": {
        "label": "Chunky (flat facets)",
        "waves": [(1, 1, 0.7), (2, 1, 0.6)],
        "ridged": False,
        "quantize": 3.0,
    },
    "cracks": {
        "label": "Cracks (sharp creases)",
        "waves": [(2, 2, 0.8), (3, -2, 0.7), (2, 3, 0.6)],
        "ridged": True,
        "quantize": 0.0,
    },
}


# ==========================================================================
# Pure helpers: PNG read/write. These never touch the Painter API, so they can
# be tested outside Painter (see the bottom of this file).
# ==========================================================================
def _png_chunk(tag, data):
    """Build a single PNG chunk: length + tag + data + CRC32."""
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(file_path, width, height, rgb_bytes):
    """Write an 8-bit RGB PNG file without any third party library.

    Painter's embedded Python ships without PIL/numpy, but the standard library
    has `zlib` and `struct`, which is all a PNG needs.
    """
    expected = width * height * 3
    if len(rgb_bytes) != expected:
        raise ValueError(
            "rgb_bytes has %d bytes, expected %d." % (len(rgb_bytes), expected))

    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 ("None")
        raw += rgb_bytes[y * stride:(y + 1) * stride]

    ihdr = struct.pack(">IIBBBBB",
                       width, height,
                       8,   # bit depth
                       2,   # color type 2 = truecolor (RGB)
                       0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" +
           _png_chunk(b"IHDR", ihdr) +
           _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           _png_chunk(b"IEND", b""))
    with open(file_path, "wb") as handle:
        handle.write(png)


def read_png(file_path):
    """Read an 8-bit PNG and return (width, height, rgb_bytes).

    Supports greyscale, greyscale+alpha, RGB and RGBA at 8 bits per channel,
    which covers the maps Painter's `alg.mapexport.save` produces. Only the
    standard library is used.
    """
    with open(file_path, "rb") as handle:
        data = handle.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG file: %s" % file_path)

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # 4 length + 4 tag + data + 4 CRC
        if tag == b"IHDR":
            (width, height, bit_depth, color_type,
             _comp, _filt, _interlace) = struct.unpack(">IIBBBBB", chunk)
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break

    if bit_depth != 8:
        raise ValueError("Only 8-bit PNGs are supported (got %s)." % bit_depth)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError("Unsupported PNG color type: %s" % color_type)

    raw = zlib.decompress(bytes(idat))
    stride = width * channels

    # Undo the per-scanline filters (PNG spec: None/Sub/Up/Average/Paeth).
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pointer = 0
    for y in range(height):
        filter_type = raw[pointer]
        pointer += 1
        line = bytearray(raw[pointer:pointer + stride])
        pointer += stride
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                line[i] = (line[i] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # Flatten to RGB.
    rgb = bytearray(width * height * 3)
    j = 0
    for i in range(0, len(out), channels):
        if channels == 1 or channels == 2:
            value = out[i]
            rgb[j] = value
            rgb[j + 1] = value
            rgb[j + 2] = value
        else:
            rgb[j] = out[i]
            rgb[j + 1] = out[i + 1]
            rgb[j + 2] = out[i + 2]
        j += 3
    return width, height, bytes(rgb)


# ==========================================================================
# Pure helpers: height-field generation and normal encoding.
# ==========================================================================
def _build_waves(style, rnd, scale, octaves):
    """Build a list of (fu, fv, amplitude, phase) waves for a style.

    `scale` multiplies the base integer frequencies and `octaves` adds
    higher-frequency, lower-amplitude copies (fractal detail). All frequencies
    stay integers, which keeps the result tileable.
    """
    definition = STYLE_DEFINITIONS[style]
    waves = []
    amplitude = 1.0
    for octave in range(max(1, octaves)):
        factor = scale * (2 ** octave)
        for base_fu, base_fv, base_amp in definition["waves"]:
            fu = base_fu * factor
            fv = base_fv * factor
            phase = rnd.uniform(0.0, TWO_PI)
            waves.append((fu, fv, base_amp * amplitude, phase))
        amplitude *= 0.5
    return waves


def _procedural_height(style, size, seed, scale, octaves):
    """Return a [0, 1] height field for a procedural style, row-major."""
    definition = STYLE_DEFINITIONS[style]
    rnd = random.Random(seed)
    waves = _build_waves(style, rnd, scale, octaves)
    ridged = definition["ridged"]

    values = [0.0] * (size * size)
    index = 0
    for y in range(size):
        v = y / float(size)
        for x in range(size):
            u = x / float(size)
            accumulator = 0.0
            for fu, fv, amplitude, phase in waves:
                wave = math.sin(TWO_PI * (fu * u + fv * v) + phase)
                if ridged:
                    wave = 1.0 - abs(wave)
                accumulator += amplitude * wave
            values[index] = accumulator
            index += 1

    _normalize(values)
    quantize = definition["quantize"]
    if quantize:
        for i, value in enumerate(values):
            values[i] = round(value * quantize) / quantize
    return values


def _luminance_height(width, height, rgb_bytes):
    """Turn an RGB image into a [0, 1] luminance height field, row-major."""
    count = width * height
    values = [0.0] * count
    for i in range(count):
        r = rgb_bytes[i * 3] / 255.0
        g = rgb_bytes[i * 3 + 1] / 255.0
        b = rgb_bytes[i * 3 + 2] / 255.0
        values[i] = 0.2126 * r + 0.7152 * g + 0.0722 * b
    _normalize(values)
    return values


def _normalize(values):
    """Rescale a list of floats in place so it spans the [0, 1] range."""
    low = min(values)
    high = max(values)
    span = (high - low) or 1.0
    for i, value in enumerate(values):
        values[i] = (value - low) / span


def _apply_tone(values, invert, contrast):
    """Apply invert and contrast (around 0.5) to a [0, 1] height field."""
    for i, value in enumerate(values):
        if invert:
            value = 1.0 - value
        value = (value - 0.5) * contrast + 0.5
        values[i] = 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _height_to_normal(values, size, strength):
    """Convert a [0, 1] height field into tileable normal-map RGB bytes.

    Finite differences with wrap-around sampling (for tileability) give the
    slope in U and V; the slope becomes the X/Y tilt of the normal.
    """
    out = bytearray(size * size * 3)
    index = 0
    for y in range(size):
        y_up = (y - 1) % size
        y_down = (y + 1) % size
        for x in range(size):
            x_left = (x - 1) % size
            x_right = (x + 1) % size

            dh_dx = values[y * size + x_right] - values[y * size + x_left]
            dh_dy = values[y_down * size + x] - values[y_up * size + x]

            nx = -dh_dx * strength
            ny = -dh_dy * strength
            nz = 1.0
            length = math.sqrt(nx * nx + ny * ny + nz * nz)

            out[index] = int(max(0, min(255, (nx / length * 0.5 + 0.5) * 255.0)))
            out[index + 1] = int(max(0, min(255, (ny / length * 0.5 + 0.5) * 255.0)))
            out[index + 2] = int(max(0, min(255, (nz / length * 0.5 + 0.5) * 255.0)))
            index += 3
    return bytes(out)


def generate_normal_map(source, style, size, strength, scale, octaves, seed,
                        detail, contrast, invert, base_rgb=None):
    """Generate a stylized normal map as raw RGB bytes.

    Args:
        source (str): "procedural", "material" or "hybrid".
        style (str): Key into STYLE_DEFINITIONS (procedural pattern).
        size (int): Square output size in pixels.
        strength (float): Bump strength multiplier.
        scale (int): Pattern frequency multiplier.
        octaves (int): Number of fractal detail levels.
        seed (int): Random seed for the procedural pattern.
        detail (float): 0..1 mix of pattern detail into the base material.
        contrast (float): Height contrast multiplier (1.0 = unchanged).
        invert (bool): Invert the height field.
        base_rgb (bytes): Required when source is "material" or "hybrid";
            the base color image (size * size * 3 bytes, same size).

    Returns:
        bytes: Row-major RGB data, length = size * size * 3.
    """
    needs_material = source in ("material", "hybrid")
    needs_pattern = source in ("procedural", "hybrid")

    material_height = None
    if needs_material:
        if base_rgb is None:
            raise ValueError("base_rgb is required for the '%s' source." % source)
        material_height = _luminance_height(size, size, base_rgb)

    pattern_height = None
    if needs_pattern:
        pattern_height = _procedural_height(style, size, seed, scale, octaves)

    if source == "procedural":
        heights = pattern_height
    elif source == "material":
        heights = material_height
    else:  # hybrid
        heights = [(1.0 - detail) * m + detail * p
                   for m, p in zip(material_height, pattern_height)]
        _normalize(heights)

    _apply_tone(heights, invert, contrast)
    return _height_to_normal(heights, size, strength)


# ==========================================================================
# Painter-facing helpers.
# ==========================================================================
def _shelf_subdir(name):
    """Return (and create if needed) a sub-folder inside the user shelf."""
    shelf_path = substance_painter.resource.Shelves.user_shelf().path()
    folder = os.path.join(shelf_path, name)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def _require_project():
    """Return True if a project is open, otherwise print a hint."""
    if not substance_painter.project.is_open():
        print("No project is open. Create or open a project first.")
        return False
    return True


def _active_texture_set():
    """Return the active TextureSet, or None with a printed message."""
    try:
        return substance_painter.textureset.get_active_stack().material()
    except Exception as error:
        print("Could not read the active Texture Set: %s" % error)
        return None


def _export_base_color(texture_set_name, size, dest_path):
    """Render the Texture Set's base color to a PNG using the JS map export.

    Returns True on success. This is how the "base material" sources get the
    pixels that are currently painted, since the Python API cannot read them
    directly.
    """
    js_path = dest_path.replace("\\", "/")
    js_code = (
        "(function(){"
        "  try {"
        "    alg.mapexport.save(%s, %s,"
        "      {resolution:[%d, %d], bitDepth:8, dithering:'disabled'});"
        "    return JSON.stringify({ok:true});"
        "  } catch (e) {"
        "    return JSON.stringify({ok:false, error:'' + e});"
        "  }"
        "})()"
    ) % (json.dumps([texture_set_name, "base color"]), json.dumps(js_path),
         size, size)

    try:
        result = substance_painter.js.evaluate(js_code)
    except Exception as error:
        print("Base color export failed: %s" % error)
        return False
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error") if isinstance(result, dict) else result
        print("Base color export failed: %s" % detail)
        print("(Does this Texture Set have a base color channel?)")
        return False
    return True


def _read_ui_settings():
    """Collect the current panel settings into a dict."""
    source = _source_combo.currentData()
    return {
        "source": source,
        "style": _pattern_combo.currentData(),
        "size": int(_size_combo.currentText()),
        "strength": float(_strength_spin.value()),
        "scale": int(_scale_spin.value()),
        "octaves": int(_octaves_spin.value()),
        "detail": _detail_spin.value() / 100.0,
        "contrast": _contrast_spin.value() / 100.0,
        "seed": int(_seed_spin.value()),
        "invert": _invert_check.isChecked(),
    }


def _generate_and_apply(*_):
    """Generate a normal map, save it, import it and set it as the mesh map."""
    if not _require_project():
        return

    texture_set = _active_texture_set()
    if texture_set is None:
        return
    settings = _read_ui_settings()

    style_label = STYLE_DEFINITIONS[settings["style"]]["label"]
    print("Generating %s normal map (%s, %dx%d, seed %d)..." % (
        settings["source"], style_label, settings["size"], settings["size"],
        settings["seed"]))

    base_rgb = None
    if settings["source"] in ("material", "hybrid"):
        temp_dir = os.path.join(tempfile.gettempdir(), "stylized_normals")
        if not os.path.isdir(temp_dir):
            os.makedirs(temp_dir)
        base_path = os.path.join(temp_dir, "basecolor.png")
        print("Reading base color from '%s'..." % texture_set.name())
        if not _export_base_color(texture_set.name(), settings["size"], base_path):
            return
        width, height, base_rgb = read_png(base_path)
        if (width, height) != (settings["size"], settings["size"]):
            print("Note: base color came back as %dx%d; using it as is."
                  % (width, height))
            # Regenerate the height field at the size we actually got.
            settings["size"] = width

    try:
        rgb = generate_normal_map(
            settings["source"], settings["style"], settings["size"],
            settings["strength"], settings["scale"], settings["octaves"],
            settings["seed"], settings["detail"], settings["contrast"],
            settings["invert"], base_rgb)
    except Exception as error:
        print("Generation failed: %s" % error)
        return

    # 1) Save into the user shelf so the map persists and is browsable.
    file_name = "stylized_normal_%s_%s_%d_%d.png" % (
        settings["source"], settings["style"], settings["size"], settings["seed"])
    textures_dir = _shelf_subdir("textures")
    shelf_file = os.path.join(textures_dir, file_name)
    write_png(shelf_file, settings["size"], settings["size"], rgb)
    substance_painter.resource.Shelves.refresh_all()
    print("Saved to shelf: %s" % shelf_file)

    # 2) Import into the project, then assign it as the Normal mesh map.
    try:
        resource = substance_painter.resource.import_project_resource(
            shelf_file,
            substance_painter.resource.Usage.TEXTURE,
            name=os.path.splitext(file_name)[0])
    except Exception as error:
        print("Importing into the project failed: %s" % error)
        return

    try:
        texture_set.set_mesh_map_resource(
            substance_painter.textureset.MeshMapUsage.Normal,
            resource.identifier())
        print("Applied to the Normal mesh map of '%s'." % texture_set.name())
    except Exception as error:
        print("Could not set the Normal mesh map: %s" % error)
        print("The texture is still in your shelf; drag it onto the Normal "
              "channel to use it manually.")
        substance_painter.resource.show_resources_in_ui([resource])
        return

    substance_painter.resource.show_resources_in_ui([resource])


def _revert_normal_map(*_):
    """Unset the Normal mesh map so the project goes back to its bake."""
    if not _require_project():
        return
    texture_set = _active_texture_set()
    if texture_set is None:
        return
    try:
        texture_set.set_mesh_map_resource(
            substance_painter.textureset.MeshMapUsage.Normal, None)
        print("Cleared the Normal mesh map of '%s'." % texture_set.name())
    except Exception as error:
        print("Could not clear the Normal mesh map: %s" % error)


def _clear_output(*_):
    """Empty the output box."""
    if _output_box is not None:
        _output_box.clear()


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


def _show_panel(*_):
    """Bring the panel back into view (used by the button and menu)."""
    if _dock is not None:
        _dock.show()
        _dock.raise_()
    elif _panel is not None:
        _panel.show()


def _build_panel():
    """Create the panel widget and return it."""
    global _output_box, _source_combo, _pattern_combo, _size_combo
    global _strength_spin, _scale_spin, _octaves_spin, _detail_spin
    global _contrast_spin, _seed_spin, _invert_check

    panel = QtWidgets.QWidget()
    panel.setObjectName(PANEL_NAME)  # Lets Painter save/restore dock geometry.
    panel.setWindowTitle("Stylized Normals")
    panel.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
        QtWidgets.QStyle.SP_FileDialogDetailedView))

    layout = QtWidgets.QVBoxLayout(panel)
    layout.setSpacing(6)

    info = QtWidgets.QLabel(
        "Generate a stylized normal map and apply it straight to the Normal "
        "mesh map of the active Texture Set.\n"
        "Base it on a procedural pattern, on your painted base color, or mix "
        "the two.")
    info.setWordWrap(True)
    layout.addWidget(info)

    # --- Source + pattern --------------------------------------------------
    source_group = QtWidgets.QGroupBox("Height source")
    form = QtWidgets.QFormLayout(source_group)

    _source_combo = QtWidgets.QComboBox()
    _source_combo.addItem("Procedural pattern", "procedural")
    _source_combo.addItem("Base material (base color)", "material")
    _source_combo.addItem("Base material + pattern detail", "hybrid")
    form.addRow("Source", _source_combo)

    _pattern_combo = QtWidgets.QComboBox()
    for key, definition in STYLE_DEFINITIONS.items():
        _pattern_combo.addItem(definition["label"], key)
    form.addRow("Pattern", _pattern_combo)
    layout.addWidget(source_group)

    # --- Generation controls ----------------------------------------------
    controls_group = QtWidgets.QGroupBox("Generation controls")
    form2 = QtWidgets.QFormLayout(controls_group)

    _size_combo = QtWidgets.QComboBox()
    _size_combo.addItems(["256", "512", "1024", "2048"])
    _size_combo.setCurrentText("1024")
    form2.addRow("Size", _size_combo)

    _strength_spin = QtWidgets.QSpinBox()
    _strength_spin.setRange(1, 20)
    _strength_spin.setValue(4)
    form2.addRow("Strength", _strength_spin)

    _scale_spin = QtWidgets.QSpinBox()
    _scale_spin.setRange(1, 16)
    _scale_spin.setValue(4)
    form2.addRow("Pattern scale", _scale_spin)

    _octaves_spin = QtWidgets.QSpinBox()
    _octaves_spin.setRange(1, 6)
    _octaves_spin.setValue(3)
    form2.addRow("Octaves (detail)", _octaves_spin)

    _detail_spin = QtWidgets.QSpinBox()
    _detail_spin.setRange(0, 100)
    _detail_spin.setValue(50)
    _detail_spin.setSuffix(" %")
    form2.addRow("Pattern mix (hybrid)", _detail_spin)

    _contrast_spin = QtWidgets.QSpinBox()
    _contrast_spin.setRange(25, 400)
    _contrast_spin.setValue(100)
    _contrast_spin.setSuffix(" %")
    form2.addRow("Contrast", _contrast_spin)

    _seed_spin = QtWidgets.QSpinBox()
    _seed_spin.setRange(0, 999999)
    _seed_spin.setValue(1)
    form2.addRow("Seed", _seed_spin)

    _invert_check = QtWidgets.QCheckBox("Invert height")
    form2.addRow(_invert_check)
    layout.addWidget(controls_group)

    # --- Actions -----------------------------------------------------------
    generate_button = QtWidgets.QPushButton("Generate & apply to Normal map")
    generate_button.clicked.connect(_generate_and_apply)
    layout.addWidget(generate_button)

    revert_button = QtWidgets.QPushButton("Revert Normal mesh map")
    revert_button.clicked.connect(_revert_normal_map)
    layout.addWidget(revert_button)

    # --- Output ------------------------------------------------------------
    _output_box = QtWidgets.QPlainTextEdit()
    _output_box.setReadOnly(True)
    _output_box.setMinimumHeight(160)
    layout.addWidget(_output_box)

    clear_button = QtWidgets.QPushButton("Clear output")
    clear_button.clicked.connect(_clear_output)
    layout.addWidget(clear_button)

    return panel


def _add_toolbar_button():
    """Add a one-click 'Stylized Normals' button to the plugins toolbar."""
    global _toolbar_button

    button = QtWidgets.QToolButton()
    button.setText("Stylized Normals")
    button.setToolTip("Show the Stylized Normals panel")
    button.clicked.connect(_show_panel)
    try:
        substance_painter.ui.add_plugins_toolbar_widget(button)
        _toolbar_button = button
    except Exception:
        button.deleteLater()  # Older builds without the plugins toolbar.


def _add_menu_action():
    """Add a 'Show Stylized Normals panel' entry to the Window menu."""
    global _menu_action

    action = QtWidgets.QAction("Show Stylized Normals panel")
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

    # Route print() output into the panel so the tools are easy to follow.
    _original_stdout = sys.stdout
    sys.stdout = _OutputRedirect(_output_box, _original_stdout)
    sys.stderr = _OutputRedirect(_output_box, sys.__stderr__)

    print("Stylized Normals toolkit ready.")


def close_plugin():
    """Called by Painter when the plugin is disabled."""
    global _original_stdout, _panel, _dock, _toolbar_button, _menu_action
    global _output_box, _source_combo, _pattern_combo, _size_combo
    global _strength_spin, _scale_spin, _octaves_spin, _detail_spin
    global _contrast_spin, _seed_spin, _invert_check

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
    _output_box = None
    _source_combo = None
    _pattern_combo = None
    _size_combo = None
    _strength_spin = None
    _scale_spin = None
    _octaves_spin = None
    _detail_spin = None
    _contrast_spin = None
    _seed_spin = None
    _invert_check = None


if __name__ == "__main__":
    start_plugin()
