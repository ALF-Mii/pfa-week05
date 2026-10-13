"""Stylized Normals toolkit for Substance 3D Painter.

A single-file plugin that adds a "Stylized Normals" panel to Substance 3D
Painter. It bundles the three things you need to get a hand-painted / stylized
look driven by surface normals:

  1. Normal channel setup
     Makes sure the active Texture Set has a Normal channel (and optionally a
     Height channel) configured so you can paint normal detail by hand.

  2. Procedural stylized normal maps
     Generates tileable, hand-painted style normal maps (painterly strokes,
     chunky facets, waves, cracks) as PNG images and imports them into the
     project so you can drag them onto a fill layer's Normal channel.

  3. A stylized custom shader
     Installs a GLSL surface shader into your shelf that shades the model with
     quantized "toon" lighting taken from the painted normals. It also offers a
     "Normal debug" view so you can see the painted normals directly.

--------------------------------------------------------------------------
Why the panel does what it does (an important limitation)
--------------------------------------------------------------------------
Substance 3D Painter's scripting APIs (the Python `substance_painter` package
and the legacy JavaScript `alg.*` API) can read and change *channels*, *mesh
maps*, *resolution*, *resources* and *shaders*, but they cannot create or edit
paint/fill *layers* or *effects* in the layer stack. So this toolkit can't draw
normal strokes into the layer stack for you; instead it sets up the Normal
channel, generates normal maps you can apply, and gives you a stylized shader.

Install:
  1. Copy this file into Painter's Python plugin folder, for example:
     .../Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
  2. Start Substance 3D Painter.
  3. Open the "Python" menu and enable "stylized_normals".
  4. The "Stylized Normals" panel appears; drag it wherever you like (or click
     the toolbar button / use Window > "Show Stylized Normals panel").

Painter 10.1+ uses Qt6 (PySide6) while older versions use Qt5 (PySide2). This
file picks the right binding automatically.
"""
import json
import math
import os
import random
import struct
import sys
import zlib

import substance_painter.application
import substance_painter.js
import substance_painter.logging
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

SHADER_FILE_NAME = "stylized_normals.glsl"  # Name of the shader on disk / shelf.

# Module level handles (kept so we can clean everything up on unload).
_output_box = None  # Text box that mirrors print() output.
_original_stdout = None  # Saved so print() can be restored on unload.
_panel = None  # The widget we dock.
_dock = None  # The QDockWidget Painter wraps it in (used to re-show it).
_toolbar_button = None  # The one-click button on the plugins toolbar.
_menu_action = None  # The Window-menu entry.
_style_combo = None  # Normal-map style selector.
_size_combo = None  # Normal-map size selector.
_strength_spin = None  # Normal-map strength selector.
_seed_spin = None  # Normal-map random seed selector.


# ==========================================================================
# Pure helpers: PNG writing and normal-map generation.
#
# These functions never touch the Painter API, which means they can be tested
# outside of Painter (see the bottom of this file for a self test).
# ==========================================================================
def _png_chunk(tag, data):
    """Build a single PNG chunk: length + tag + data + CRC32."""
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(file_path, width, height, rgb_bytes):
    """Write an 8-bit RGB PNG file without any third party library.

    Painter's embedded Python ships without PIL/numpy, but the standard library
    has `zlib` and `struct`, which is all a PNG needs.

    Args:
        file_path (str): Destination file path.
        width (int): Image width in pixels.
        height (int): Image height in pixels.
        rgb_bytes (bytes): Row-major RGB data, length = width * height * 3.
    """
    expected = width * height * 3
    if len(rgb_bytes) != expected:
        raise ValueError(
            "rgb_bytes has %d bytes, expected %d." % (len(rgb_bytes), expected))

    # Raw image = one filter byte (0 = "None") followed by each row of pixels.
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb_bytes[y * stride:(y + 1) * stride]

    ihdr = struct.pack(">IIBBBBB",
                       width, height,
                       8,   # bit depth
                       2,   # color type 2 = truecolor (RGB)
                       0,   # compression method
                       0,   # filter method
                       0)   # interlace method
    png = (b"\x89PNG\r\n\x1a\n" +
           _png_chunk(b"IHDR", ihdr) +
           _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           _png_chunk(b"IEND", b""))
    with open(file_path, "wb") as handle:
        handle.write(png)


def _make_waves(rnd, count, fu_range, fv_range):
    """Pick a handful of tileable sine waves (integer frequencies).

    Integer frequencies make the pattern repeat seamlessly, so the resulting
    normal map is tileable across the UV island.

    Returns:
        list[tuple]: (fu, fv, amplitude, phase) for each wave.
    """
    waves = []
    for _ in range(count):
        fu = rnd.randint(fu_range[0], fu_range[1])
        fv = rnd.randint(fv_range[0], fv_range[1])
        if fu == 0 and fv == 0:
            fu = 1
        # Higher frequencies get a smaller amplitude, like fBm noise.
        amplitude = 1.0 / math.sqrt(fu * fu + fv * fv + 1.0)
        phase = rnd.uniform(0.0, 2.0 * math.pi)
        waves.append((fu, fv, amplitude, phase))
    return waves


def _height_field(style, size, seed):
    """Build a [0, 1] height field for the requested style.

    Args:
        style (str): One of the keys of STYLE_DESCRIPTIONS.
        size (int): Width and height in pixels (square).
        seed (int): Random seed for reproducibility.

    Returns:
        list[float]: size * size values in the [0, 1] range, row-major.
    """
    rnd = random.Random(seed)
    two_pi = 2.0 * math.pi

    if style == "painterly":
        waves = _make_waves(rnd, 5, (1, 2), (3, 6))  # stretched strokes
    elif style == "chunky":
        waves = _make_waves(rnd, 4, (1, 3), (1, 3))
    elif style == "cracks":
        waves = _make_waves(rnd, 7, (2, 5), (2, 5))
    else:  # "waves"
        waves = _make_waves(rnd, 5, (1, 3), (1, 3))

    values = [0.0] * (size * size)
    index = 0
    for y in range(size):
        v = y / float(size)
        for x in range(size):
            u = x / float(size)
            accumulator = 0.0
            if style == "cracks":
                # Ridged pattern: sharp crests look like painted cracks.
                for fu, fv, amplitude, phase in waves:
                    accumulator += amplitude * (
                        1.0 - abs(math.sin(two_pi * (fu * u + fv * v) + phase)))
            else:
                for fu, fv, amplitude, phase in waves:
                    accumulator += amplitude * math.sin(
                        two_pi * (fu * u + fv * v) + phase)
            values[index] = accumulator
            index += 1

    # Normalize to [0, 1].
    low = min(values)
    high = max(values)
    span = (high - low) or 1.0
    for i, value in enumerate(values):
        values[i] = (value - low) / span

    # Chunky style: quantize into flat facets for a folded-paper look.
    if style == "chunky":
        levels = 5.0
        for i, value in enumerate(values):
            values[i] = round(value * levels) / levels

    return values


def generate_normal_map(style, size, strength, seed):
    """Generate a tileable normal map (as raw RGB bytes) from a height field.

    Args:
        style (str): Style key (see STYLE_DESCRIPTIONS).
        size (int): Square image size in pixels.
        strength (float): Bump strength multiplier.
        seed (int): Random seed.

    Returns:
        bytes: Row-major RGB data, length = size * size * 3.
    """
    heights = _height_field(style, size, seed)
    out = bytearray(size * size * 3)
    index = 0
    for y in range(size):
        y_up = (y - 1) % size
        y_down = (y + 1) % size
        for x in range(size):
            x_left = (x - 1) % size
            x_right = (x + 1) % size

            dh_dx = heights[y * size + x_right] - heights[y * size + x_left]
            dh_dy = heights[y_down * size + x] - heights[y_up * size + x]

            nx = -dh_dx * strength
            ny = -dh_dy * strength
            nz = 1.0
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            nx /= length
            ny /= length
            nz /= length

            out[index] = int(max(0, min(255, (nx * 0.5 + 0.5) * 255.0)))
            out[index + 1] = int(max(0, min(255, (ny * 0.5 + 0.5) * 255.0)))
            out[index + 2] = int(max(0, min(255, (nz * 0.5 + 0.5) * 255.0)))
            index += 3
    return bytes(out)


STYLE_DESCRIPTIONS = {
    "painterly": "Painterly strokes (stretched ridges)",
    "waves": "Waves (soft rounded bumps)",
    "chunky": "Chunky (flat folded facets)",
    "cracks": "Cracks (sharp painted creases)",
}


# ==========================================================================
# The stylized shader that gets written into the shelf.
# ==========================================================================
STYLIZED_SHADER = r'''// Stylized normals shader - generated by stylized_normals.py
//
// Shades the model with a quantized ("toon") light response plus a live,
// procedural stylized-normal pattern. Switching the "Pattern" combobox in
// Display Settings > Shaders (or in the shader's properties) updates the
// viewport immediately - no need to regenerate or re-apply any bitmap.
//
// Pick it in Display Settings > Shaders, or use the "Apply stylized shader"
// button in the toolkit panel.

import lib-sampler.glsl
import lib-normal.glsl

//: param auto world_eye_position
uniform vec3 camera_pos;

//: param auto channel_basecolor
uniform SamplerSparse basecolor_tex;

//: param custom {
//:   "default": 0,
//:   "label": "Shading mode",
//:   "widget": "combobox",
//:   "values": {
//:     "Toon": 0,
//:     "Flat (unlit)": 1,
//:     "Normal debug": 2
//:   }
//: }
uniform int shading_mode;

//: param custom {
//:   "default": 4,
//:   "label": "Pattern",
//:   "widget": "combobox",
//:   "values": {
//:     "Painterly": 0,
//:     "Waves": 1,
//:     "Chunky": 2,
//:     "Cracks": 3,
//:     "Off (painted normals only)": 4
//:   }
//: }
uniform int pattern;

//: param custom { "default": 6.0, "min": 0.5, "max": 40.0, "label": "Pattern scale" }
uniform float pattern_scale;

//: param custom { "default": 0.25, "min": 0.0, "max": 2.0, "label": "Pattern strength" }
uniform float pattern_strength;

//: param custom { "default": 4.0, "min": 2.0, "max": 24.0, "label": "Toon steps" }
uniform float toon_steps;

//: param custom { "default": 1.0, "min": 0.0, "max": 3.0, "label": "Light intensity" }
uniform float light_intensity;

//: param custom { "default": 0.15, "min": 0.0, "max": 1.0, "label": "Ambient" }
uniform float ambient;

//: param custom { "default": 0.0, "min": 0.0, "max": 0.5, "label": "Outline (0 = off)" }
uniform float outline_thickness;

const vec3 light_dir = normalize(vec3(0.4, 0.8, 0.6));
const float TWO_PI = 6.2831853;

// A tileable height field for each stylized pattern. Same idea as the maps the
// Python toolkit generates (sums of integer-frequency sine waves), evaluated
// live in the shader so the style can change without regenerating a bitmap.
float patternHeight(vec2 uv, int style)
{
  float value = 0.0;

  if (style == 0) {
    // Painterly: few, stretched, low-frequency strokes.
    value += 0.9 * sin(TWO_PI * (1.0 * uv.x + 3.0 * uv.y) + 0.5);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x + 5.0 * uv.y) + 2.1);
    value += 0.4 * sin(TWO_PI * (1.0 * uv.x + 8.0 * uv.y) + 4.4);
  } else if (style == 1) {
    // Waves: soft rounded bumps.
    value += 0.8 * sin(TWO_PI * (1.0 * uv.x + 1.0 * uv.y) + 0.3);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x - 1.0 * uv.y) + 1.7);
    value += 0.5 * sin(TWO_PI * (1.0 * uv.x + 2.0 * uv.y) + 3.9);
  } else if (style == 2) {
    // Chunky: quantized into flat folded facets.
    value += 0.7 * sin(TWO_PI * (1.0 * uv.x + 1.0 * uv.y) + 1.0);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x + 1.0 * uv.y) + 2.5);
    value = floor(value * 3.0 + 0.5) / 3.0;
  } else {
    // Cracks: ridged pattern with sharp painted creases.
    value += 0.8 * (1.0 - abs(sin(TWO_PI * (2.0 * uv.x + 2.0 * uv.y) + 0.4)));
    value += 0.7 * (1.0 - abs(sin(TWO_PI * (3.0 * uv.x - 2.0 * uv.y) + 1.9)));
    value += 0.6 * (1.0 - abs(sin(TWO_PI * (2.0 * uv.x + 3.0 * uv.y) + 3.3)));
  }
  return value;
}

void shade(V2F inputs)
{
  vec3 V = normalize(camera_pos - inputs.position);
  vec3 tangent = normalize(inputs.tangent);
  vec3 bitangent = normalize(inputs.bitangent);

  // World-space normal from the painted Normal channel (+ height / base normal).
  vec3 N = computeWSNormal(inputs.sparse_coord, tangent, bitangent,
                           normalize(inputs.normal));

  // Perturb that normal with the chosen procedural pattern (unless "Off").
  if (pattern != 4 && pattern_strength > 0.0) {
    const float eps = 0.01;
    vec2 uv = inputs.sparse_coord.tex_coord * pattern_scale;
    float h = patternHeight(uv, pattern);
    float hx = patternHeight(uv + vec2(eps, 0.0), pattern);
    float hy = patternHeight(uv + vec2(0.0, eps), pattern);
    vec2 slope = vec2((hx - h) / eps, (hy - h) / eps) * pattern_strength;
    N = normalize(N - (tangent * slope.x + bitangent * slope.y));
  }

  // The lit viewport is built from diffuseShadingOutput() + specularShadingOutput()
  // (the same outputs the built-in shaders use). albedoOutput() alone only shows
  // in the "Base Color" debug view, so we never rely on it here.

  // Mode 2: show the (patterned) normal directly as a colour.
  if (shading_mode == 2) {
    diffuseShadingOutput(clamp(N * 0.5 + 0.5, 0.0, 1.0));
    specularShadingOutput(vec3(0.0));
    return;
  }

  vec3 base = getBaseColor(basecolor_tex, inputs.sparse_coord);

  // Mode 1: flat, unlit colour (useful to check the painted base).
  if (shading_mode == 1) {
    diffuseShadingOutput(base);
    specularShadingOutput(vec3(0.0));
    return;
  }

  // Mode 0: toon shading - quantize the diffuse term into hard bands.
  float NdL = max(0.0, dot(N, light_dir));

  // Optional drawn outline: leaving the outputs untouched renders black.
  float NdV = max(0.0, dot(N, V));
  if (outline_thickness > 0.0 && NdV < outline_thickness) {
    return;
  }

  float steps = max(2.0, toon_steps);
  float band = floor(NdL * steps) / steps;
  float lighting = mix(ambient, 1.0, band) * light_intensity;

  vec3 color = base * lighting;
  diffuseShadingOutput(clamp(color, 0.0, 1.0));
  specularShadingOutput(vec3(0.0));
}
'''


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


def _setup_normal_channel(*_):
    """Make sure the active stack has a Normal (and Height) channel."""
    if not _require_project():
        return

    try:
        stack = substance_painter.textureset.get_active_stack()
    except Exception as error:  # No active stack yet.
        print("Could not get the active stack: %s" % error)
        return

    textureset = stack.material()
    if textureset.is_layered_material():
        print("This Texture Set uses material layering; channels are managed "
              "by the shader and cannot be added here.")
        return

    channel_type = substance_painter.textureset.ChannelType
    channel_format = substance_painter.textureset.ChannelFormat

    # Normal channel: add it as an 8-bit RGB map if it is missing.
    if stack.has_channel(channel_type.Normal):
        normal = stack.get_channel(channel_type.Normal)
        print("Normal channel already exists (format: %s)." % normal.format())
    else:
        stack.add_channel(channel_type.Normal, channel_format.RGB8)
        print("Added a Normal channel (RGB8).")

    # Height channel (L8) is handy for sculpting bumps that feed the normal.
    if stack.has_channel(channel_type.Height):
        print("Height channel already exists.")
    else:
        stack.add_channel(channel_type.Height, channel_format.L8)
        print("Added a Height channel (L8).")

    print("Ready: paint on the Normal / Height channels for a stylized look.")


def _generate_normal_texture(*_):
    """Generate a stylized normal map, save it to the shelf and import it."""
    if not _require_project():
        return

    style = _style_combo.currentData() if _style_combo is not None else "painterly"
    size = int(_size_combo.currentText()) if _size_combo is not None else 512
    strength = float(_strength_spin.value()) if _strength_spin is not None else 3.0
    seed = int(_seed_spin.value()) if _seed_spin is not None else 1

    file_name = "stylized_normal_%s_%d_%d.png" % (style, size, seed)
    print("Generating %s normal map %dx%d (seed %d)..." % (style, size, size, seed))

    rgb = generate_normal_map(style, size, strength, seed)

    # 1) Save the PNG inside the user shelf so it shows up in the Assets window.
    textures_dir = _shelf_subdir("textures")
    shelf_file = os.path.join(textures_dir, file_name)
    write_png(shelf_file, size, size, rgb)
    substance_painter.resource.Shelves.refresh_all()
    print("Saved to shelf: %s" % shelf_file)

    # 2) Embed a copy in the project as a texture resource.
    try:
        resource = substance_painter.resource.import_project_resource(
            shelf_file,
            substance_painter.resource.Usage.TEXTURE,
            name=os.path.splitext(file_name)[0])
        substance_painter.resource.show_resources_in_ui([resource])
        print("Imported into the project and highlighted in the Assets window.")
    except Exception as error:
        print("Shelf copy saved, but importing into the project failed: %s" % error)

    print("Drag the texture onto a fill layer's Normal channel "
          "(or add a Fill layer, then set its Normal channel).")


def _install_shader():
    """Write the stylized shader into the user shelf."""
    shaders_dir = _shelf_subdir("shaders")
    shader_path = os.path.join(shaders_dir, SHADER_FILE_NAME)
    with open(shader_path, "w", encoding="utf-8") as handle:
        handle.write(STYLIZED_SHADER)
    substance_painter.resource.Shelves.refresh_all()
    return shader_path


def _find_shader_resource():
    """Find the installed shader as a Painter resource, or return None."""
    stem = os.path.splitext(SHADER_FILE_NAME)[0]
    try:
        shelf = substance_painter.resource.Shelves.user_shelf()
        candidates = shelf.resources()
    except Exception:
        candidates = []
    for resource in candidates:
        try:
            identifier = resource.identifier()
        except Exception:
            continue
        name = (identifier.name or "").lower()
        if name == SHADER_FILE_NAME.lower() or name == stem.lower():
            return resource
    return None


def _install_shader_clicked(*_):
    """Button handler: install the shader and explain how to use it."""
    path = _install_shader()
    print("Installed stylized shader: %s" % path)
    print("Use 'Apply stylized shader' to switch the viewport, or pick it in "
          "Display Settings > Shaders.")


def _apply_shader(*_):
    """Switch the project's shader instances over to the stylized shader."""
    if not _require_project():
        return

    resource = _find_shader_resource()
    if resource is None:
        print("Shader not found in the shelf. Click 'Install stylized shader' first.")
        return

    shader_url = resource.identifier().url()

    # Drive the legacy JavaScript API through Python. `updateShaderInstance`
    # replaces the shader used by a shader instance (shaderId, shaderUrl).
    js_code = (
        "(function(){"
        "  var url = %s;"
        "  var result = [];"
        "  var shaders = alg.shaders.instances();"
        "  for (var i = 0; i < shaders.length; i++) {"
        "    try {"
        "      alg.shaders.updateShaderInstance(shaders[i].id, url);"
        "      result.push({id: shaders[i].id, label: shaders[i].label, ok: true});"
        "    } catch (e) {"
        "      result.push({id: shaders[i].id, label: shaders[i].label,"
        "                  ok: false, error: '' + e});"
        "    }"
        "  }"
        "  return JSON.stringify(result);"
        "})()"
    ) % json.dumps(shader_url)

    try:
        results = substance_painter.js.evaluate(js_code)
    except Exception as error:
        print("Applying the shader failed: %s" % error)
        return

    if not results:
        print("No shader instance found to update.")
        return
    for entry in results:
        state = "ok" if entry.get("ok") else "failed: %s" % entry.get("error")
        print("Shader instance %s (%s): %s" %
              (entry.get("id"), entry.get("label"), state))
    print("Done. If the look did not change, pick the shader manually in "
          "Display Settings > Shaders.")


def _clear_output(*_):
    """Empty the output box."""
    if _output_box is not None:
        _output_box.clear()


# ==========================================================================
# Panel / UI wiring (mirrors painter_bridge.py conventions).
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
    global _output_box, _style_combo, _size_combo, _strength_spin, _seed_spin

    panel = QtWidgets.QWidget()
    panel.setObjectName(PANEL_NAME)  # Lets Painter save/restore dock geometry.
    panel.setWindowTitle("Stylized Normals")
    panel.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
        QtWidgets.QStyle.SP_FileDialogDetailedView))

    layout = QtWidgets.QVBoxLayout(panel)
    layout.setSpacing(6)

    info = QtWidgets.QLabel(
        "Tools for a hand-painted, stylized look driven by normal maps.\n"
        "Painter's scripting cannot create paint layers, so generate maps here "
        "and paint on the Normal channel for the rest.")
    info.setWordWrap(True)
    layout.addWidget(info)

    # --- Group 1: Normal channel setup ------------------------------------
    group1 = QtWidgets.QGroupBox("1. Normal channel")
    box1 = QtWidgets.QVBoxLayout(group1)
    setup_button = QtWidgets.QPushButton("Set up Normal channel")
    setup_button.clicked.connect(_setup_normal_channel)
    box1.addWidget(setup_button)
    layout.addWidget(group1)

    # --- Group 2: Stylized normal maps ------------------------------------
    group2 = QtWidgets.QGroupBox("2. Stylized normal maps")
    form = QtWidgets.QFormLayout(group2)

    _style_combo = QtWidgets.QComboBox()
    for key, label in STYLE_DESCRIPTIONS.items():
        _style_combo.addItem(label, key)
    form.addRow("Style", _style_combo)

    _size_combo = QtWidgets.QComboBox()
    _size_combo.addItems(["256", "512", "1024"])
    _size_combo.setCurrentText("512")
    form.addRow("Size", _size_combo)

    _strength_spin = QtWidgets.QSpinBox()
    _strength_spin.setRange(1, 10)
    _strength_spin.setValue(3)
    form.addRow("Strength", _strength_spin)

    _seed_spin = QtWidgets.QSpinBox()
    _seed_spin.setRange(0, 999999)
    _seed_spin.setValue(1)
    form.addRow("Seed", _seed_spin)

    generate_button = QtWidgets.QPushButton("Generate & import normal map")
    generate_button.clicked.connect(_generate_normal_texture)
    form.addRow(generate_button)
    layout.addWidget(group2)

    # --- Group 3: Stylized shader -----------------------------------------
    group3 = QtWidgets.QGroupBox("3. Stylized shader")
    box3 = QtWidgets.QVBoxLayout(group3)
    install_button = QtWidgets.QPushButton("Install stylized shader")
    install_button.clicked.connect(_install_shader_clicked)
    box3.addWidget(install_button)
    apply_button = QtWidgets.QPushButton("Apply stylized shader to viewport")
    apply_button.clicked.connect(_apply_shader)
    box3.addWidget(apply_button)
    hint3 = QtWidgets.QLabel(
        "Tip: the shader has its own live controls. In Display Settings > "
        "Shaders, set Pattern to Painterly / Waves / Chunky / Cracks to change "
        "the stylized normal straight away. Use 'Normal debug' from Shading "
        "mode to see it clearly.")
    hint3.setWordWrap(True)
    box3.addWidget(hint3)
    layout.addWidget(group3)

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
    global _output_box, _style_combo, _size_combo, _strength_spin, _seed_spin

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
    _style_combo = None
    _size_combo = None
    _strength_spin = None
    _seed_spin = None


if __name__ == "__main__":
    start_plugin()
