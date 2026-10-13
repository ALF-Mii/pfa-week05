# Substance 3D Painter Bridge

This repo's centrepiece is a **live bridge into Adobe Substance 3D Painter**: a
small server that runs *inside* Painter and a terminal client that sends it
commands. An agent (or anyone) can drive the running app from a terminal and
watch it react. The other scripts here (Maya, Houdini) are separate teaching
examples.

## The bridge: app and way

- **App:** Adobe Substance 3D Painter (not Maya).
- **Way:** a **port**. Painter has no external scripting and no official MCP
  server - its Python API only runs inside the app. So the plugin opens a TCP
  port on `127.0.0.1:7301`, and a terminal client sends Python that runs live in
  the app. This is the same idea as Maya's command port: commands go *into* the
  open app and you watch the result.

Two files make up the bridge:

| File | Role |
| --- | --- |
| `painter_bridge.py` | Painter plugin. Opens the port, runs incoming code on the main thread, and shows a panel that logs every command. |
| `painter_client.py` | Terminal client. Connects to the port, sends a request, prints whatever the app sends back. |

## How to run it

Do this once to install, then use the client for everything.

1. **Install the plugin.** Copy `painter_bridge.py` (and, for the demo below,
   `stylized_normals.py`) into Painter's Python plugin folder. The folder name
   repeats "Adobe":

   ```
   C:\Users\<you>\Documents\Adobe\Adobe Substance 3D Painter\python\plugins\
   ```

   (`Python > Plugins Folder` in Painter shows the exact path; create `plugins\`
   if it isn't there.)

2. **Start Painter and enable the plugin.** Open the **Python** menu and tick
   **painter_bridge** (and **stylized_normals**). Once enabled, Painter
   remembers it and starts it automatically from then on. The panel shows
   `Status: listening on 127.0.0.1:7301`.

3. **Send commands from a terminal.** Use Painter's own bundled Python (always
   present, no install needed):

   ```powershell
   $py = "C:\Program Files (x86)\Steam\steamapps\common\Substance 3D Painter 2023\resources\pythonsdk\python.exe"
   & $py painter_client.py --action ping
   ```

   If your Painter is installed elsewhere, point `$py` at its
   `resources\pythonsdk\python.exe`. (Any Python 3 also works - the client only
   uses the standard library.) If you changed the port in the plugin, pass
   `--port` or set the `PAINTER_BRIDGE_PORT` environment variable.

### Commands

```powershell
# Is the app listening?
python painter_client.py --action ping

# What project is open?
python painter_client.py --action project_info

# Create a demo project from a small bundled sample mesh
python painter_client.py --action create_project

# Run arbitrary Python inside the live app (prints and the last
# expression both come back to your terminal)
python painter_client.py -c "print(project.name())"

# Run a whole file inside the app
python painter_client.py -f my_job.py

# THE ONE THING: apply the stylized shader to the open model
python painter_client.py --action stylize --type Toon --style Painterly
python painter_client.py --action stylize --type Toon --style Painterly --set style_scale=8

# Undo it: back to Painter's default shading
python painter_client.py --action reset_shading
```

Available `--action`s: `ping`, `project_info`, `create_project`, `stylize`,
`reset_shading`; plus raw code via `-c`/`-f`.

## What I asked for, and what happened

**What I asked for (exact words):** _"Give this model a hand-drawn toon look"_

**What happened** (real session, driven from the terminal through the port):

```text
$ python painter_client.py --action ping
'pong'

$ python painter_client.py --action create_project
'Created a project from ...\automated_tests\resources\4udims.obj'

$ python painter_client.py --action stylize --type Toon --style Painterly
Applying shader: resource://your_assets/stylized_normals?version=e58330f0...shader
Applied to 1 shader instance(s).
"Applied shader type 'Toon' with style 'Painterly'."
```

Checking the app from the terminal afterwards confirms the live change:

```text
$ python painter_client.py -c "import substance_painter.js as j; print(j.evaluate('(function(){var l=alg.shaders.instances();return {ok:true,s:l[0].shader};})()'))"
{'ok': True, 's': 'stylized_normals'}
```

The model in the viewport switched to the stylized toon/painterly look, live,
with no clicking in the app.

## Recording (one minute or less)

The screen capture should show the terminal **and** Painter side by side, then: 
https://drive.google.com/file/d/1aDZLCLMAqJoc6Cri3Qv6NVkKYWaK3sP0/view?usp=sharing


---

## Tools in this repo

| Script | Host | What it does |
| --- | --- | --- |
| `painter_bridge.py` | Substance 3D Painter | **The bridge.** TCP port + panel that runs commands from the terminal live. |
| `painter_client.py` | (terminal) | Sends commands/files to the bridge and prints the app's reply. |
| `stylized_normals.py` | Substance 3D Painter | Installs and applies a custom stylized surface shader with many Shader types and live procedural Styles. |

## Requirements

- Adobe Substance 3D Painter (10.1+ for Qt6/PySide6, older for Qt5/PySide2) for
  the bridge and the stylized shader.
- Autodesk Maya (any recent version with Python) for the Maya tools.
- SideFX Houdini for the star & glitter sim. The POPs are DOP-based, so a build
  with the full FX node set is required (tested on Houdini 19.0).
- No third-party packages. Everything uses the standard library plus each host's
  own scripting API.

## How the bridge works (internals)

- The plugin's `BridgeServer` thread only does network I/O. Each request is put
  on a `queue.Queue`; a `QTimer` on the main thread drains it and runs the code
  with `_run_request`, because the Qt and `substance_painter` APIs must be used
  on the main thread.
- `print()` output and the value of the final expression are captured and sent
  back to the client; exceptions come back as a traceback.
- Requests are newline-delimited JSON: `{"code": "..."}` or
  `{"action": "...", "args": {...}}`, answered with
  `{"ok": ..., "stdout": ..., "result": ..., "error": ...}`.

## Stylized shader (`stylized_normals.py`)

Install it the same way as the bridge: copy it into the same `python/plugins/`
folder, then enable **stylized_normals** in the **Python** menu. A **Stylized
Shader** panel appears (with a toolbar button and a **Window > Show Stylized
Shader panel** entry).

The plugin installs a custom stylized surface shader into your user shelf
(`assets/shaders/`) and applies it to the viewport. It packs many looks behind
two comboboxes:

- **Shader type** - `Toon`, `Flat (unlit)`, `Normal debug`, `Hatching`,
  `Halftone`, `Gooch (warm/cool)`, `Rim glow`, `X-ray`.
- **Style** - a bank of procedural patterns that perturb the normal *live*, so
  switching styles updates the viewport instantly (no bitmap to bake):
  `Painterly`, `Waves`, `Chunky`, `Cracks`, `Stripes`, `Scales`, `Rings`,
  `Noise`, `Fabric` (or `Off`).

Panel controls:

- **Shader type** / **Style** combos - switch those two parameters live.
- **Fine-tuning sliders** - one per shader parameter (**Style scale**, **Style
  strength**, **Toon steps**, **Light intensity**, **Ambient**, **Outline**,
  **Rim/X-ray strength** & **falloff**, **Hatch density**, **Halftone density**,
  **Posterize**, **Saturation**, **Hue shift**). Drags are debounced.
- **Install / update shader** - writes/replaces the shader in the shelf.
- **Apply to viewport** - switches the project's shader instances over to it
  (remembers the previous shader).
- **Reset to normal shading** - puts Painter's default PBR shader back.
- **Restore previous shader** - puts back exactly the shader that was active
  before.

It is also what the bridge's `stylize` / `reset_shading` actions call, so the
same look can be driven from the terminal.

> **How applying works:** the plugin writes the `.glsl` and uses the JavaScript
> API (`alg.shaders.updateShaderInstance`) to point each shader instance at it.
> The combos and sliders use `alg.shaders.setParameters`.

