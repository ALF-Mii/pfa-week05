"""Stylized shader toolkit for Substance 3D Painter.

A single-file plugin that installs a custom *stylized surface shader* into your
user shelf and applies it to the viewport. The shader packs many looks behind
two comboboxes:

  * Shader type - Toon, Flat (unlit), Normal debug, Hatching, Halftone,
    Gooch (warm/cool), Rim glow, X-ray.
  * Style - a bank of procedural patterns (Painterly, Waves, Chunky, Cracks,
    Stripes, Scales, Rings, Noise, Fabric) that perturb the normal *live*, so
    switching styles updates the viewport instantly - no bitmap to bake.

The panel mirrors those two combos for quick switching, adds sliders for every
tunable parameter, and has buttons to install/update the shader, apply it to the
viewport, reset back to normal (default PBR) shading, and restore whatever
shader was active before. Every parameter is also shown in
**Display Settings > Shaders**.

Install:
  1. Copy this file into Painter's Python plugin folder, for example:
     .../Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
  2. Start Substance 3D Painter.
  3. Open the "Python" menu and enable "stylized_normals".
  4. The "Stylized Shader" panel appears (also a toolbar button and a
     Window > "Show Stylized Shader panel" entry).

Painter 10.1+ uses Qt6 (PySide6) while older versions use Qt5 (PySide2). This
file picks the right binding automatically.
"""
import io
import json
import os
import sys
import time

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

PANEL_NAME = "StylizedShaderPanel"  # Unique name so Painter remembers the dock.
_END = QtGui.QTextCursor.MoveOperation.End  # Cursor position: end of document.

SHADER_NAME = "stylized_normals"
SHADER_FILE = "stylized_normals.glsl"

# The two combobox option lists. They must match the shader's own combobox
# definitions (see STYLIZED_SHADER below) so the panel and the Display Settings
# panel stay in sync.
SHADER_TYPES = [
    ("Toon", 0),
    ("Flat (unlit)", 1),
    ("Normal debug", 2),
    ("Hatching", 3),
    ("Halftone", 4),
    ("Gooch (warm/cool)", 5),
    ("Rim glow", 6),
    ("X-ray", 7),
]
STYLE_OPTIONS = [
    ("Off (painted normals only)", 0),
    ("Painterly", 1),
    ("Waves", 2),
    ("Chunky", 3),
    ("Cracks", 4),
    ("Stripes", 5),
    ("Scales", 6),
    ("Rings", 7),
    ("Noise", 8),
    ("Fabric", 9),
]

# The fine-tuning sliders shown in the panel. Each entry is
#   (identifier, label, minimum, maximum, step, default, decimals)
# They map 1:1 to the float uniforms in STYLIZED_SHADER, so moving a slider
# pushes the value straight into the live shader (same thing you'd do by hand
# in Display Settings > Shaders, just handier). Keep the ranges in sync with
# the "min"/"max" hints in the shader source above.
PANEL_PARAMS = [
    ("style_scale", "Style scale", 0.5, 40.0, 0.5, 6.0, 1),
    ("style_strength", "Style strength", 0.0, 2.0, 0.05, 0.25, 2),
    ("toon_steps", "Toon steps", 2.0, 24.0, 0.5, 4.0, 1),
    ("light_intensity", "Light intensity", 0.0, 3.0, 0.05, 1.0, 2),
    ("ambient", "Ambient", 0.0, 1.0, 0.02, 0.15, 2),
    ("outline_thickness", "Outline (0 = off)", 0.0, 0.5, 0.01, 0.0, 2),
    ("rim_strength", "Rim / X-ray strength", 0.0, 2.0, 0.05, 0.5, 2),
    ("rim_power", "Rim / X-ray falloff", 0.5, 8.0, 0.1, 2.0, 1),
    ("hatch_scale", "Hatch density", 4.0, 120.0, 1.0, 24.0, 0),
    ("screen_scale", "Halftone density", 4.0, 120.0, 1.0, 24.0, 0),
    ("posterize", "Posterize (0 = off)", 0.0, 12.0, 1.0, 0.0, 0),
    ("saturation", "Saturation", 0.0, 3.0, 0.05, 1.0, 2),
    ("hue_shift", "Hue shift (rad)", -3.14, 3.14, 0.02, 0.0, 2),
]

# Module level handles (kept so we can clean everything up on unload).
_output_box = None  # Text box that mirrors print() output.
_original_stdout = None  # Saved so print() can be restored on unload.
_panel = None  # The widget we dock.
_dock = None  # The QDockWidget Painter wraps it in (used to re-show it).
_toolbar_button = None  # The one-click button on the plugins toolbar.
_menu_action = None  # The Window-menu entry.
_shader_type_combo = None  # Shader type combo in the panel.
_style_combo = None  # Style combo in the panel.
_ui_ready = False  # Suppresses combo callbacks while the panel is being built.
_previous_shader_urls = None  # Instance urls saved before we switch shader.
_sliders = {}  # identifier -> (QSlider, QLabel) for the fine-tuning sliders.
_slider_timer = None  # Debounces slider drags into a single shader update.
_pending_params = {}  # identifier -> value waiting to be pushed to the shader.

# ==========================================================================
# The shader itself. Written verbatim into the user shelf when installed.
# ==========================================================================
STYLIZED_SHADER = """// Stylized shader - generated by stylized_normals.py
//
// A single stylized surface shader with many "Shader type" looks and a bank of
// procedural "Style" patterns that perturb the normal live. Change the combos
// in Display Settings > Shaders (or the plugin panel) and the viewport updates
// instantly - no need to regenerate or re-apply any bitmap.
//
// Pick it in Display Settings > Shaders, or use the "Apply to viewport" button
// in the toolkit panel.

import lib-sampler.glsl
import lib-normal.glsl

//: param auto world_eye_position
uniform vec3 camera_pos;

//: param auto channel_basecolor
uniform SamplerSparse basecolor_tex;

//: param custom {
//:   "default": 0,
//:   "label": "Shader type",
//:   "widget": "combobox",
//:   "values": {
//:     "Toon": 0,
//:     "Flat (unlit)": 1,
//:     "Normal debug": 2,
//:     "Hatching": 3,
//:     "Halftone": 4,
//:     "Gooch (warm/cool)": 5,
//:     "Rim glow": 6,
//:     "X-ray": 7
//:   }
//: }
uniform int shader_type;

//: param custom {
//:   "default": 0,
//:   "label": "Style",
//:   "widget": "combobox",
//:   "values": {
//:     "Off (painted normals only)": 0,
//:     "Painterly": 1,
//:     "Waves": 2,
//:     "Chunky": 3,
//:     "Cracks": 4,
//:     "Stripes": 5,
//:     "Scales": 6,
//:     "Rings": 7,
//:     "Noise": 8,
//:     "Fabric": 9
//:   }
//: }
uniform int style;

//: param custom { "default": 6.0, "min": 0.5, "max": 40.0, "label": "Style scale" }
uniform float style_scale;

//: param custom { "default": 0.25, "min": 0.0, "max": 2.0, "label": "Style strength" }
uniform float style_strength;

//: param custom { "default": 4.0, "min": 2.0, "max": 24.0, "label": "Toon steps" }
uniform float toon_steps;

//: param custom { "default": 1.0, "min": 0.0, "max": 3.0, "label": "Light intensity" }
uniform float light_intensity;

//: param custom { "default": 0.15, "min": 0.0, "max": 1.0, "label": "Ambient" }
uniform float ambient;

//: param custom { "default": 0.0, "min": 0.0, "max": 0.5, "label": "Outline (0 = off)" }
uniform float outline_thickness;

//: param custom { "default": 0.5, "min": 0.0, "max": 2.0, "label": "Rim / X-ray strength" }
uniform float rim_strength;

//: param custom { "default": 2.0, "min": 0.5, "max": 8.0, "label": "Rim / X-ray falloff" }
uniform float rim_power;

//: param custom { "default": 24.0, "min": 4.0, "max": 120.0, "label": "Hatch density" }
uniform float hatch_scale;

//: param custom { "default": 24.0, "min": 4.0, "max": 120.0, "label": "Halftone density" }
uniform float screen_scale;

//: param custom { "default": 0.0, "min": 0.0, "max": 12.0, "label": "Posterize (0 = off)" }
uniform float posterize;

//: param custom { "default": 1.0, "min": 0.0, "max": 3.0, "label": "Saturation" }
uniform float saturation;

//: param custom { "default": 0.0, "min": -3.14, "max": 3.14, "label": "Hue shift (rad)" }
uniform float hue_shift;

const vec3 light_dir = normalize(vec3(0.4, 0.8, 0.6));
const float TWO_PI = 6.2831853;

// Rotate the hue of a colour around the grey axis (Rodrigues rotation).
vec3 hueShift(vec3 col, float angle)
{
  const vec3 k = vec3(0.57735027, 0.57735027, 0.57735027);
  float ca = cos(angle);
  float sa = sin(angle);
  return col * ca + cross(k, col) * sa + k * dot(k, col) * (1.0 - ca);
}

// A tileable height field for each procedural style, evaluated live so the
// style can change without regenerating a bitmap.
float patternHeight(vec2 uv, int s)
{
  float value = 0.0;
  if (s == 1) {
    // Painterly: few, stretched, low-frequency strokes.
    value += 0.9 * sin(TWO_PI * (1.0 * uv.x + 3.0 * uv.y) + 0.5);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x + 5.0 * uv.y) + 2.1);
    value += 0.4 * sin(TWO_PI * (1.0 * uv.x + 8.0 * uv.y) + 4.4);
  } else if (s == 2) {
    // Waves: soft rounded bumps.
    value += 0.8 * sin(TWO_PI * (1.0 * uv.x + 1.0 * uv.y) + 0.3);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x - 1.0 * uv.y) + 1.7);
    value += 0.5 * sin(TWO_PI * (1.0 * uv.x + 2.0 * uv.y) + 3.9);
  } else if (s == 3) {
    // Chunky: quantized into flat folded facets.
    value += 0.7 * sin(TWO_PI * (1.0 * uv.x + 1.0 * uv.y) + 1.0);
    value += 0.6 * sin(TWO_PI * (2.0 * uv.x + 1.0 * uv.y) + 2.5);
    value = floor(value * 3.0 + 0.5) / 3.0;
  } else if (s == 4) {
    // Cracks: ridged pattern with sharp painted creases.
    value += 0.8 * (1.0 - abs(sin(TWO_PI * (2.0 * uv.x + 2.0 * uv.y) + 0.4)));
    value += 0.7 * (1.0 - abs(sin(TWO_PI * (3.0 * uv.x - 2.0 * uv.y) + 1.9)));
    value += 0.6 * (1.0 - abs(sin(TWO_PI * (2.0 * uv.x + 3.0 * uv.y) + 3.3)));
  } else if (s == 5) {
    // Stripes.
    value += 0.9 * sin(TWO_PI * (uv.x * 6.0) + 1.0);
    value += 0.4 * sin(TWO_PI * (uv.x * 6.0 + uv.y * 2.0) + 2.0);
  } else if (s == 6) {
    // Scales: crossed diagonal ridges (diamond grid).
    value += 0.7 * sin(TWO_PI * (uv.x * 4.0 + uv.y * 4.0));
    value += 0.7 * sin(TWO_PI * (uv.x * 4.0 - uv.y * 4.0));
  } else if (s == 7) {
    // Rings: concentric circles inside each tile.
    vec2 cell = fract(uv * 3.0) - 0.5;
    value += 0.9 * sin(length(cell) * 18.0);
  } else if (s == 8) {
    // Noise: a small fractal sum.
    value += 0.6 * sin(TWO_PI * (1.0 * uv.x + 1.0 * uv.y) + 0.7);
    value += 0.4 * sin(TWO_PI * (3.0 * uv.x - 2.0 * uv.y) + 2.3);
    value += 0.3 * sin(TWO_PI * (6.0 * uv.x + 5.0 * uv.y) + 4.1);
    value += 0.2 * sin(TWO_PI * (11.0 * uv.x - 9.0 * uv.y) + 1.3);
  } else if (s == 9) {
    // Fabric: a woven grid.
    value += 0.6 * sin(TWO_PI * (uv.x * 8.0));
    value += 0.6 * sin(TWO_PI * (uv.y * 8.0) + 1.5708);
    value *= 0.5;
  }
  return value;
}

// Triangle wave for hatching lines at a given angle.
float hatchLines(vec2 uv, float angle, float freq)
{
  float c = cos(angle);
  float s = sin(angle);
  vec2 r = vec2(uv.x * c - uv.y * s, uv.x * s + uv.y * c);
  return abs(fract(r.y * freq) - 0.5) * 2.0;
}

void shade(V2F inputs)
{
  vec3 V = normalize(camera_pos - inputs.position);
  vec3 tangent = normalize(inputs.tangent);
  vec3 bitangent = normalize(inputs.bitangent);

  // World-space normal from the painted Normal channel (+ height / base normal).
  vec3 N = computeWSNormal(inputs.sparse_coord, tangent, bitangent,
                           normalize(inputs.normal));

  // Perturb that normal with the chosen procedural style (unless "Off").
  if (style != 0 && style_strength > 0.0) {
    const float eps = 0.01;
    vec2 uv = inputs.sparse_coord.tex_coord * style_scale;
    float h = patternHeight(uv, style);
    float hx = patternHeight(uv + vec2(eps, 0.0), style);
    float hy = patternHeight(uv + vec2(0.0, eps), style);
    vec2 slope = vec2((hx - h) / eps, (hy - h) / eps) * style_strength;
    N = normalize(N - (tangent * slope.x + bitangent * slope.y));
  }

  vec3 base = getBaseColor(basecolor_tex, inputs.sparse_coord);

  float NdL = max(0.0, dot(N, light_dir));
  float NdV = max(0.0, dot(N, V));

  // Optional drawn outline: leaving the outputs untouched renders black.
  if (outline_thickness > 0.0 && NdV < outline_thickness) {
    return;
  }

  // The lit viewport is built from diffuseShadingOutput() + specularShadingOutput()
  // (the same outputs the built-in shaders use). albedoOutput() alone only shows
  // in the "Base Color" debug view, so we never rely on it here.
  vec3 color = base;

  if (shader_type == 2) {
    // Normal debug: show the (styled) normal directly as a colour.
    color = clamp(N * 0.5 + 0.5, 0.0, 1.0);
  } else if (shader_type == 1) {
    // Flat (unlit): the painted base colour, untouched.
    color = base;
  } else if (shader_type == 0) {
    // Toon: quantize the diffuse term into hard bands.
    float steps = max(2.0, toon_steps);
    float band = floor(NdL * steps) / steps;
    color = base * mix(ambient, 1.0, band) * light_intensity;
  } else if (shader_type == 3) {
    // Hatching: layer pen strokes as the surface turns away from the light.
    vec2 uv = inputs.sparse_coord.tex_coord * hatch_scale;
    float dark = 1.0 - NdL;
    float ink = 0.0;
    if (dark > 0.15) ink = max(ink, 1.0 - hatchLines(uv, 0.0, 1.0));
    if (dark > 0.40) ink = max(ink, 1.0 - hatchLines(uv, 0.7, 1.0));
    if (dark > 0.65) ink = max(ink, 1.0 - hatchLines(uv, 1.4, 1.0));
    color = base * (1.0 - 0.85 * ink);
  } else if (shader_type == 4) {
    // Halftone: a dot screen whose dot size follows the light.
    vec2 uv = inputs.sparse_coord.tex_coord * screen_scale;
    vec2 cell = fract(uv) - 0.5;
    float dist = length(cell);
    float radius = (1.0 - NdL) * 0.65;
    float dotMask = 1.0 - smoothstep(radius - 0.05, radius + 0.05, dist);
    color = base * mix(0.25, 1.1, dotMask);
  } else if (shader_type == 5) {
    // Gooch: a warm-to-cool lighting ramp (classic technical illustration).
    vec3 cool = vec3(0.15, 0.25, 0.65);
    vec3 warm = vec3(0.95, 0.75, 0.45);
    vec3 ramp = cool * (1.0 - NdL) + warm * NdL;
    color = base * ramp * light_intensity;
  } else if (shader_type == 6) {
    // Rim glow: soft lambert plus a fresnel rim.
    float lambert = mix(ambient, 1.0, NdL);
    float rim = pow(1.0 - NdV, max(0.5, rim_power)) * rim_strength;
    color = base * lambert * light_intensity + base * rim;
  } else if (shader_type == 7) {
    // X-ray: fresnel-brightened, see-through look.
    float fres = pow(1.0 - NdV, max(0.5, rim_power));
    color = base * (0.12 + 1.2 * fres * rim_strength);
  }

  // Optional colour grade - shared by every shader type.
  if (posterize >= 2.0) {
    color = floor(color * posterize) / posterize;
  }
  float luma = dot(color, vec3(0.2126, 0.7152, 0.0722));
  color = mix(vec3(luma), color, saturation);
  color = hueShift(color, hue_shift);

  diffuseShadingOutput(clamp(color, 0.0, 1.0));
  specularShadingOutput(vec3(0.0));
}
"""


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


def _js_eval(js_code):
    """Run JavaScript through Painter and return the decoded result.

    `substance_painter.js.evaluate` already parses the engine's result as JSON,
    but depending on how the expression returns its value the result can come
    back as a JSON string; normalise that here so callers always get a Python
    object.
    """
    result = substance_painter.js.evaluate(js_code)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            pass
    return result


def _install_shader(*_):
    """Write the stylized shader into the user shelf and refresh it."""
    try:
        folder = _shelf_subdir("shaders")
        path = os.path.join(folder, SHADER_FILE)
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(STYLIZED_SHADER)
        substance_painter.resource.Shelves.refresh_all()
    except Exception as error:
        print("Could not install the shader: %s" % error)
        return
    print("Installed shader to: %s" % path)
    print("Press 'Apply to viewport', or pick it in Display Settings > Shaders.")


def _find_shader_resource():
    """Return the shelf Resource for our shader, or None."""
    try:
        shelf = substance_painter.resource.Shelves.user_shelf()
        resources = shelf.resources()
    except Exception as error:
        print("Could not read the shelf: %s" % error)
        return None
    for resource in resources:
        try:
            if resource.type() != substance_painter.resource.Type.SHADER:
                continue
        except Exception:
            continue
        name = resource.identifier().name
        if name.split("/")[-1] == SHADER_NAME or name.endswith(SHADER_NAME):
            return resource
    return None


# Name of Painter's built-in "normal" shader. Switching back to this is how we
# undo the stylized look and return to ordinary PBR shading.
DEFAULT_SHADER_NAME = "pbr-metal-rough"


def _find_default_shader_resource():
    """Return the shelf Resource for Painter's default PBR shader, or None."""
    shelves = []
    for getter in ("application_shelf", "project_shelf"):
        try:
            shelves.append(getattr(substance_painter.resource.Shelves, getter)())
        except Exception:
            continue
    for shelf in shelves:
        try:
            resources = shelf.resources()
        except Exception as error:
            print("Could not read a shelf: %s" % error)
            continue
        for resource in resources:
            try:
                if resource.type() != substance_painter.resource.Type.SHADER:
                    continue
            except Exception:
                continue
            name = resource.identifier().name.split("/")[-1]
            if name == DEFAULT_SHADER_NAME:
                return resource
    return None


def _wait_for_shader(timeout=10.0):
    """Poll the shelf (it crawls asynchronously) until our shader appears."""
    deadline = time.time() + timeout
    while True:
        resource = _find_shader_resource()
        if resource is not None:
            return resource
        if time.time() >= deadline:
            return None
        print("Waiting for Painter to index the shader...")
        time.sleep(0.5)


def _instance_urls():
    """Return the current shader instances as a list of {id, url} dicts."""
    info = _js_eval(
        "(function(){"
        "  try {"
        "    var list = alg.shaders.instances();"
        "    var out = [];"
        "    for (var i = 0; i < list.length; i++){"
        "      out.push({id: list[i].id, url: list[i].url});"
        "    }"
        "    return {ok:true, instances: out};"
        "  } catch (e) { return {ok:false, error:'' + e}; }"
        "})()")
    if isinstance(info, dict) and info.get("ok"):
        return info.get("instances") or []
    return []


def _switch_shader(url):
    """Point every shader instance of the project at the given shader url.

    Returns the (dict) result of the JavaScript call, or None if there is no
    JavaScript bridge. Shared by "Apply to viewport" and "Reset to normal".
    """
    return _js_eval(
        "(function(){"
        "  try {"
        "    var list = alg.shaders.instances();"
        "    for (var i = 0; i < list.length; i++){"
        "      alg.shaders.updateShaderInstance(list[i].id, %s);"
        "    }"
        "    return {ok:true, count: list.length};"
        "  } catch (e) { return {ok:false, error:'' + e}; }"
        "})()" % json.dumps(url))


def _apply_shader(*_):
    """Switch every shader instance of the project over to our shader."""
    if not _require_project():
        return

    resource = _wait_for_shader(6.0)
    if resource is None:
        print("Could not find '%s' in the shelf." % SHADER_NAME)
        print("Press 'Install / update shader' first, then try again.")
        return

    # Remember the current shaders so "Restore previous shader" can undo this.
    global _previous_shader_urls
    _previous_shader_urls = _instance_urls()

    url = resource.identifier().url()
    print("Applying shader: %s" % url)
    result = _switch_shader(url)

    if isinstance(result, dict) and result.get("ok"):
        print("Applied to %d shader instance(s)." % result.get("count", 0))
        print("Open Display Settings > Shaders to tweak it.")
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        print("Could not apply the shader automatically: %s" % detail)
        print("You can select 'stylized_normals' manually in "
              "Display Settings > Shaders.")


def _restore_shader(*_):
    """Put back the shader instances that were active before we applied ours."""
    if not _require_project():
        return
    if not _previous_shader_urls:
        print("No previous shader was recorded. Nothing to restore.")
        return

    pairs = ", ".join("%s: %s" % (json.dumps(str(item["id"])),
                                  json.dumps(str(item["url"])))
                      for item in _previous_shader_urls)
    result = _js_eval(
        "(function(){"
        "  try {"
        "    var map = { %s };"
        "    var list = alg.shaders.instances();"
        "    for (var i = 0; i < list.length; i++){"
        "      if (map[list[i].id]) {"
        "        alg.shaders.updateShaderInstance(list[i].id, map[list[i].id]);"
        "      }"
        "    }"
        "    return {ok:true};"
        "  } catch (e) { return {ok:false, error:'' + e}; }"
        "})()" % pairs)

    if isinstance(result, dict) and result.get("ok"):
        _previous_shader_urls = None
        print("Restored the previous shader.")
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        print("Could not restore the previous shader: %s" % detail)


def _reset_shading(*_):
    """Return the viewport to ordinary PBR shading (undo the stylized look)."""
    global _previous_shader_urls

    if not _require_project():
        return

    resource = _find_default_shader_resource()
    if resource is None:
        # We could not find the built-in shader; fall back to whatever was
        # active before we applied ours (if we know).
        if _previous_shader_urls:
            print("Could not find the default shader; restoring the previous "
                  "one instead.")
            _restore_shader()
            return
        print("Could not find the default shader ('%s')." % DEFAULT_SHADER_NAME)
        print("Pick any shader manually in Display Settings > Shaders.")
        return

    url = resource.identifier().url()
    print("Returning to the default shader: %s" % url)
    result = _switch_shader(url)
    if isinstance(result, dict) and result.get("ok"):
        # The stylized look is gone, so our "Restore previous" is spent too.
        _previous_shader_urls = None
        print("Back to normal shading (%d instance(s))." % result.get("count", 0))
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        print("Could not switch back automatically: %s" % detail)
        print("You can select a shader manually in Display Settings > Shaders.")


def _set_parameters(values, quiet=False):
    """Set several shader parameters on every instance running our shader."""
    if not values:
        return
    pairs = ", ".join("%s: %s" % (json.dumps(str(key)), json.dumps(val))
                      for key, val in values.items())
    js_code = (
        "(function(){"
        "  try {"
        "    var list = alg.shaders.instances();"
        "    var changed = 0;"
        "    for (var i = 0; i < list.length; i++){"
        "      if (String(list[i].shader).indexOf(%s) >= 0){"
        "        alg.shaders.setParameters(list[i].id, { %s });"
        "        changed++;"
        "      }"
        "    }"
        "    return {ok:true, count: changed};"
        "  } catch (e) { return {ok:false, error:'' + e}; }"
        "})()" % (json.dumps(SHADER_NAME), pairs))
    result = _js_eval(js_code)
    if not (isinstance(result, dict) and result.get("ok")):
        detail = result.get("error") if isinstance(result, dict) else result
        print("Could not set %s: %s" % (list(values), detail))
        return
    if not result.get("count") and not quiet:
        print("The stylized shader is not active. Press 'Apply to viewport'.")


def _set_parameter(identifier, value):
    """Set one shader parameter (thin wrapper around _set_parameters)."""
    _set_parameters({identifier: value})


def _on_combo_changed(identifier, combo):
    """Callback for the panel's Shader type / Style combos."""
    if not _ui_ready or combo is None:
        return
    if not substance_painter.project.is_open():
        print("No project is open.")
        return
    _set_parameter(identifier, combo.currentData())


def _on_slider_changed(identifier, value, label, decimals):
    """Callback for a fine-tuning slider.

    The label updates immediately; the shader update is debounced through a
    short timer so dragging a slider sends one change per pause instead of a
    flood of them.
    """
    label.setText(("%." + str(decimals) + "f") % value)
    if not _ui_ready:
        return
    _pending_params[identifier] = value
    if _slider_timer is not None:
        _slider_timer.start()


def _push_pending_params():
    """Send every parameter queued by the sliders to the live shader."""
    global _pending_params
    values = _pending_params
    _pending_params = {}
    if values and substance_painter.project.is_open():
        _set_parameters(values, quiet=True)


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


def _make_slider(identifier, label, minimum, maximum, step, default, decimals):
    """Build one slider row for a shader parameter and add it to a form layout.

    A QSlider only works in integers, so we map its 0..N range onto the real
    float range (minimum .. maximum) in steps of `step`.
    """
    steps = int(round((maximum - minimum) / step))
    slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
    slider.setMinimum(0)
    slider.setMaximum(steps)
    slider.setValue(int(round((default - minimum) / step)))

    value_label = QtWidgets.QLabel(("%." + str(decimals) + "f") % default)
    value_label.setMinimumWidth(46)
    value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    def on_change(position):
        value = minimum + position * step
        _on_slider_changed(identifier, value, value_label, decimals)

    slider.valueChanged.connect(on_change)

    row = QtWidgets.QWidget()
    row_layout = QtWidgets.QHBoxLayout(row)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(6)
    row_layout.addWidget(slider, 1)
    row_layout.addWidget(value_label)

    _sliders[identifier] = (slider, value_label)
    return label, row


def _build_panel():
    """Create the panel widget and return it."""
    global _output_box, _shader_type_combo, _style_combo, _slider_timer, _sliders

    content = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(content)
    layout.setSpacing(6)

    info = QtWidgets.QLabel(
        "A stylized surface shader with many looks.\n"
        "Install it, apply it to the viewport, then pick a Shader type and a "
        "Style and fine-tune with the sliders below.")
    info.setWordWrap(True)
    layout.addWidget(info)

    # --- Live controls -----------------------------------------------------
    live_group = QtWidgets.QGroupBox("Live controls")
    form = QtWidgets.QFormLayout(live_group)

    _shader_type_combo = QtWidgets.QComboBox()
    for label, value in SHADER_TYPES:
        _shader_type_combo.addItem(label, value)
    _shader_type_combo.currentIndexChanged.connect(
        lambda index: _on_combo_changed("shader_type", _shader_type_combo))
    form.addRow("Shader type", _shader_type_combo)

    _style_combo = QtWidgets.QComboBox()
    for label, value in STYLE_OPTIONS:
        _style_combo.addItem(label, value)
    _style_combo.currentIndexChanged.connect(
        lambda index: _on_combo_changed("style", _style_combo))
    form.addRow("Style", _style_combo)
    layout.addWidget(live_group)

    # --- Fine-tuning sliders ----------------------------------------------
    fine_group = QtWidgets.QGroupBox("Fine-tuning")
    fine_form = QtWidgets.QFormLayout(fine_group)
    _sliders = {}
    for params in PANEL_PARAMS:
        label, row = _make_slider(*params)
        fine_form.addRow(label, row)
    layout.addWidget(fine_group)

    # Debounces slider drags so one pause sends one shader update, not dozens.
    _slider_timer = QtCore.QTimer()
    _slider_timer.setSingleShot(True)
    _slider_timer.setInterval(120)
    _slider_timer.timeout.connect(_push_pending_params)

    # --- Actions -----------------------------------------------------------
    install_button = QtWidgets.QPushButton("Install / update shader")
    install_button.setToolTip("Write the shader into your user shelf")
    install_button.clicked.connect(_install_shader)
    layout.addWidget(install_button)

    apply_button = QtWidgets.QPushButton("Apply to viewport")
    apply_button.setToolTip("Switch the viewport over to the stylized shader")
    apply_button.clicked.connect(_apply_shader)
    layout.addWidget(apply_button)

    reset_button = QtWidgets.QPushButton("Reset to normal shading")
    reset_button.setToolTip(
        "Return to Painter's default shader (undo the stylized look)")
    reset_button.clicked.connect(_reset_shading)
    layout.addWidget(reset_button)

    restore_button = QtWidgets.QPushButton("Restore previous shader")
    restore_button.setToolTip(
        "Put back exactly the shader that was active before you applied this")
    restore_button.clicked.connect(_restore_shader)
    layout.addWidget(restore_button)

    # --- Output ------------------------------------------------------------
    _output_box = QtWidgets.QPlainTextEdit()
    _output_box.setReadOnly(True)
    _output_box.setMinimumHeight(140)
    layout.addWidget(_output_box)

    clear_button = QtWidgets.QPushButton("Clear output")
    clear_button.clicked.connect(_clear_output)
    layout.addWidget(clear_button)

    # The panel is long, so put everything inside a scroll area.
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setObjectName(PANEL_NAME)  # Lets Painter save/restore dock geometry.
    scroll.setWindowTitle("Stylized Shader")
    scroll.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
        QtWidgets.QStyle.SP_FileDialogDetailedView))
    scroll.setWidget(content)
    return scroll


def _add_toolbar_button():
    """Add a one-click 'Stylized Shader' button to the plugins toolbar."""
    global _toolbar_button

    button = QtWidgets.QToolButton()
    button.setText("Stylized Shader")
    button.setToolTip("Show the Stylized Shader panel")
    button.clicked.connect(_show_panel)
    try:
        substance_painter.ui.add_plugins_toolbar_widget(button)
        _toolbar_button = button
    except Exception:
        button.deleteLater()  # Older builds without the plugins toolbar.


def _add_menu_action():
    """Add a 'Show Stylized Shader panel' entry to the Window menu."""
    global _menu_action

    action = QtWidgets.QAction("Show Stylized Shader panel")
    action.triggered.connect(_show_panel)
    try:
        substance_painter.ui.add_action(
            substance_painter.ui.ApplicationMenu.Window, action)
        _menu_action = action
    except Exception:
        pass  # Not fatal: the toolbar button still works.


def start_plugin():
    """Called by Painter when the plugin is enabled."""
    global _original_stdout, _panel, _dock, _ui_ready

    _panel = _build_panel()
    _dock = substance_painter.ui.add_dock_widget(_panel)
    _add_toolbar_button()
    _add_menu_action()

    # Route print() output into the panel so the tools are easy to follow.
    _original_stdout = sys.stdout
    sys.stdout = _OutputRedirect(_output_box, _original_stdout)
    sys.stderr = _OutputRedirect(_output_box, sys.__stderr__)
    _ui_ready = True

    print("Stylized Shader toolkit ready.")


def close_plugin():
    """Called by Painter when the plugin is disabled."""
    global _original_stdout, _panel, _dock, _toolbar_button, _menu_action
    global _output_box, _shader_type_combo, _style_combo, _ui_ready
    global _sliders, _slider_timer

    _ui_ready = False

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
    _shader_type_combo = None
    _style_combo = None
    _sliders = {}
    if _slider_timer is not None:
        try:
            _slider_timer.stop()
        except Exception:
            pass
    _slider_timer = None
    _pending_params.clear()


if __name__ == "__main__":
    start_plugin()
