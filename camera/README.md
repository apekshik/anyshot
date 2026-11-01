# camrig: cinematic camera rig and shot renderer

`camera/shoot.py` adds one camera rig per shot to a scene .blend, then renders:
- preview blocking passes,
- framing checks,
- lens charts,
- final Cycles frames and mp4s.

Shots are described in a JSON file: an AnyShot order writes its own as `out/shots/<job>/shots.json`, and an AnyScene
build keeps its scouting cameras in `out/shots.json`. Examples live in `camera/examples/`.

The contract is stable. New fields may be added, but existing ones will not change meaning.

## CLI

```bash
. pipeline/env.sh   # sets $B (Blender) and $ROOT_ABS (absolute repo root)
# always through the shared GPU queue
pipeline/gpu_run.sh "$B" -b scenes/my-set/set.blend --python camera/shoot.py -- \
  --shots $ROOT_ABS/scenes/my-set/shots.json \
  --out   $ROOT_ABS/scenes/my-set/out/shots \
  [--only a,b] [--preview | --framing | --lenschart NAME] [--frames 1-24] [--still 12] [--save rigged.blend] [--no-encode] \
  [--engine eevee|cycles|workbench] [--step N]
```

| Mode | What it does | Output (`<out>/<shot>/…`) |
|---|---|---|
| *(default)* final | Renders with Cycles. Scene settings stay unless a shot field overrides them. Frames that already exist are skipped, so a run can be resumed. Encodes `<shot>.mp4` and a contact sheet once every frame exists. | `frames/####.png`, `<shot>.mp4`, `contact.png` |
| `--preview` | Renders with Eevee at 50% resolution, every frame (`--step N` renders every Nth), 16 TAA samples, no motion blur. The stamp is burned in per frame: shot name, frame, lens mm, f-stop, focus distance. `--engine cycles` gives a 16 spp denoised preview with the real lighting. | `preview/####.png`, `preview.mp4` |
| `--framing` | Renders a stamped low-res still of each shot at t = 0, 0.5 and 1 with Eevee, plus one 3×3 sheet for all shots. | `framing/t0.png`, `framing/t5.png`, `framing/t10.png`, `<out>/_framing.png` |
| `--lenschart NAME` | Renders shot NAME's t=0 position and aim at 18/24/35/50/85/135 mm with Eevee. | `lenschart/<mm>.png`, `<out>/<NAME>_lenschart.png` |
| `--frames a-b` | Renders only frames a..b of the shot (1-based). Use it to keep each `gpu_run` call short (aim for under about 10 minutes). | |
| `--still N` | Renders a single final-quality frame N. | `still_####.png` |
| `--save path.blend` | Writes the rigged scene so a human can open and tweak it in the UI. It never overwrites the input file. | |
| `--no-encode` | Skips mp4 and contact-sheet encoding. | |

The script never saves the input .blend. Every shot's rig is built fresh on each run.

## shots.json

```jsonc
{
  "defaults": { "fps": 24, "res": [1920, 1080], "aspect": "16:9", "samples": 256,
                "shutter": 180, "sensor": "s35", "fstop": 2.8, "ease": "inout", "handheld": 0.0 },
  "shots": [
    {
      "name": "counter_push",                 // [a-z0-9_], used for folders and the rig name
      "frames": 120,                          // length in frames
      "fps": 24,
      "res": [1920, 1080],
      "aspect": "2.39",                       // "16:9" | "1.85" | "2.39" — output is cropped to the aspect (res height recomputed from width)
      "samples": 256,                         // Cycles samples for final
      "shutter": 180,                         // motion blur shutter in degrees (0 = off)
      "sensor": "s35",                        // "s35" (24.89 mm) | "ff" (36 mm) | number in mm (sensor width)
      "move": "push_in",                      // see Moves
      "ease": "inout",                        // linear | in | out | inout — position/aim: one ease over the whole shot (passes through middle keys without stopping); lens/roll/focus/f-stop keys: eased per segment (hold + pull)
      "keys": [
        { "t": 0.0, "pos": [x,y,z], "aim": [x,y,z], "lens": 35, "roll": 0 },
        { "t": 1.0, "pos": [x,y,z], "aim_obj": "ramen_bowl_unfinished", "lens": 35 }
      ],
      "focus": { "mode": "rack", "keys": [ { "t": 0.0, "obj": "water_glass" }, { "t": 0.6, "pos": [x,y,z] } ] },
      "fstop": 2.0,                           // or "fstop_keys": [{ "t":0, "v":4 }, { "t":1, "v":1.4 }]
      "blades": 0, "ratio": 1.0,              // aperture blades (0 = round) and anamorphic ratio (>1 = oval bokeh)
      "handheld": 0.35,                       // 0..1, shake layered on top of the move
      "breathing": 0.5,                       // 0..1, slow drift (default = handheld)
      "lens_fx": { "distortion": 0.01, "dispersion": 0.01, "glare": "fog" }   // optional, see notes
    }
  ]
}
```

Units are metres and Blender world axes (Z up). Wherever the contract takes a position, an object name works too.
- `aim_obj` and `focus.obj` resolve to the **centre of that object's world bounding box**.
- `obj` matches the object name exactly. If nothing matches exactly, it uses the first object whose name starts with the given string, then the first case-insensitive substring match. Bounds include all mesh children, so a GLB import root works.
- `"aim_obj": "name", "aim_offset": [dx,dy,dz]` nudges the aim point from that centre. Keys also take `pos_obj` + `pos_offset`. A key that omits `pos` or `aim` holds the previous one.
- Optional `"exposure": EV` offsets the scene exposure for the shot. The scenes' old keyed exposure drift is removed and frozen at its frame-1 value, so it can't leak into new shots.

### Moves

All moves interpolate between `keys`; `move` sets defaults and validation:

| move | keys needed | notes |
|---|---|---|
| `static` | 1 | Locked off. Add `handheld` for life. |
| `dolly`, `truck`, `pedestal`, `push_in`, `crane` | 2 or more | Each key has a position and an aim. The names describe intent; they all interpolate the same way. |
| `pan`, `tilt` | 2 or more | Keep `pos` the same and change `aim`. |
| `orbit` | none (or 1 for lens and roll) | `"orbit": {"center":[x,y,z] or "center_obj":"name", "radius":1.2, "start_deg":-30, "end_deg":30, "height":1.3}`. Aims at the centre. Angles are measured from +X, counter-clockwise from above. |
| `dolly_zoom` | 2 (start and end `pos`; aim and lens at t=0) | Lens is solved per frame to hold the subject size: `lens(t) = lens0 · dist(t) / dist0`. Distance is measured to `"subject"` (pos or obj, defaults to the aim). |
| `path` | 3 or more | Smooth path through every key: Catmull-Rom on position, eased in time. |

### Focus

- `{"mode":"track","obj":"name"}`: the focus empty is parented to and follows that object's bounds centre.
- `{"mode":"point","pos":[x,y,z]}`
- `{"mode":"rack","keys":[{"t":0,"obj":"a"},{"t":0.6,"pos":[...]}]}`: the focus empty moves between points with the same ease. The pull happens between successive keys, so to hold and then pull, repeat a key: `t 0 → a`, `t 0.4 → a`, `t 0.6 → b`.
- `{"mode":"none"}`: depth of field off.
- `fstop` (or `fstop_keys`), `blades` and `ratio` are optional.

### Handheld

- **Shake (`handheld`):** two Noise layers per channel on `CR_<shot>_shake` (between aim and camera, so it moves in camera space).
  - Hand bob: period about 11 frames, 4 mm and 0.27° × intensity.
  - Fine jitter: period about 3.5 frames, 1.2 mm and 0.07° × intensity.
  - Roll runs at about 60% of pan and tilt.
- **Breathing:** body sway with a period of about 2 s, 14 mm and 0.4° × breathing.
- **Lens length:** the shift scales with focal length the way real handheld does. At 85 mm, handheld 0.4 already reads clearly; at 24 mm, 0.3–0.5 feels like a steady operator.

### Handheld walk profile (opt-in)

`"handheld": {"profile": "walk", "amount": 1.0}` replaces the noise shake with a baked operator-walking motion. Shots that keep a number for `handheld` are unchanged.

- **Footsteps:** a bob of about 1 cm, once per step at 1.8 Hz (`step_hz`), and a 0.3° roll that alternates left and right with each step.
  Both scale with the speed of the move's path: full at or above `walk_speed` (default 0.55 m/s), zero when standing.
  Walking intensity is smoothed over about 0.4 s, so steps fade in and out with the move's ease.
- **Body sway:** 0.17–0.57 Hz. Position moves ±2–3 cm and pan/tilt ±0.7°. When the operator stands still this drops to about 45%.
- **Aim lag:** the aim follows a spring (`aim_lag_hz` 0.9, `aim_damping` 0.62). Pans trail behind the keyed aim and overshoot slightly when they settle.
  Focus that follows the aim (the default focus mode) uses the lagged aim as well.
- **Reframes:** `corrections` (default 2) small reframes. Each drifts off by 0.6° and is quickly corrected back.
- **Frequency ceiling:** nothing is above about 1.9 Hz, so there is no jitter. Motion blur sees smooth curves.
- **Baking:** everything is baked per frame onto `CR_<shot>_shake` (location in metres, rotation in radians, in the camera's local axes).
  It is deterministic per shot index.
- **Setting it up:** use `move: "path"` with 4–6 keys at a walking pace (about 0.3–0.6 m/s) and `ease: "inout"`, so the operator starts and stops gently.
  Check the camera path against the scene before rendering: load the scene once, build a BVH of its meshes, and report the closest mesh and its frame for every frame of the move.

## Rig hierarchy (in the saved .blend)

```
CR_<shot>_base     (location baked one LINEAR key per frame)
 └ CR_<shot>_aim   (rotation: Track To an animated aim empty CR_<shot>_target)
    └ CR_<shot>_shake   (noise only)
       └ CR_<shot>_cam  (camera data: lens/sensor/DOF; lens animated)
CR_<shot>_target   (aim point, animated)
CR_<shot>_focus    (DOF focus object, animated or parented)
```

Moves are evaluated in Python and **baked one key per frame**, so every move type behaves the same and Cycles motion blur sees smooth subframes.

The real way to change a move is to edit shots.json and rerun. In a `--save`d file you can still:
- turn the shake empty's Noise modifiers up or down (Graph Editor → Modifiers),
- move the focus empty,
- change lens and f-stop keys on the camera data.

Each shot's rig lives in its own collection `camrig/CamRig_<shot>`. Rerunning a shot deletes and rebuilds that rig.

## Lens FX / grading (Blender 5.0)

- `lens_fx.distortion` and `lens_fx.dispersion` use the **Lens Distortion** compositor node. `lens_fx.glare` (`"fog"`, `"bloom"`, `"streaks"`) uses the **Glare** node.
- **Vignette, colour balance and grain are not done here.** The 5.0 mask and colour-balance nodes misbehave, so finish those in your own ffmpeg grade pass on the rendered frames.
- **Existing compositor:** if the scene already has one, it is kept unless the shot sets `lens_fx`. In that case camrig copies the scene's tree to `camrig_fx` and splices its nodes in just before the Group Output. (Wrapping the old tree in a group node renders **black** in 5.0, because Render Layers inside a nested group yields nothing.)

## Encoding

- ffmpeg comes from `$FFMPEG` (default `ffmpeg` on PATH). camrig encodes with `h264_nvenc` (CQ 18, yuv420p, faststart), which is fast and barely touches the GPU.
- If NVENC fails, it falls back to `h264_mf`.
- Frames are fed through an ffmpeg concat list, so no glob is needed and stepped previews work. 16-bit PNG frames encode fine.

## Helpers (python, inside Blender)

```python
import sys; sys.path.append("<repo>/camera")
import camrig
camrig.bounds("ticket_machine")      # -> (min Vector, max Vector, center Vector)
camrig.find("ramen_bowl")            # exact name or prefix match
```

`--list "pattern"` prints the matching objects with their bounds centre and size, then exits:

```bash
"$B" -b scene.blend --python camera/shoot.py -- --list bowl
```

## Measured (RTX 5070 Ti, Blender 5.0.1, on two of AnyScene's reference sets)

| Pass | Scene | Time |
|---|---|---|
| `--framing`, 4 shots → 12 Eevee stills at 640 px | either | 30–50 s, including the file load |
| `--preview`, 960×402 Eevee | ramen | ~0.25 s/frame |
| `--preview`, 960×402 Eevee | repair | ~0.35 s/frame |
| `--lenschart` | either | ~12 s |
| Final Cycles, 960×402, 32 spp, motion blur, glare + lens distortion | repair | ~10 s/frame |
| Final Cycles, 640×360, 16 spp | repair | ~3.3 s/frame |

Scale final times by pixels × samples before you pick a `--frames` chunk size.

## Robust vs flaky (Blender 5.0)

**Robust:**
- every move type, including dolly_zoom lens solving and Catmull-Rom paths
- Track To aim
- rack, track and point focus
- f-stop keys and blades/ratio
- Noise-modifier handheld
- per-frame stamps (a `frame_change_post` handler updates the note)
- Eevee previews
- resume via `use_overwrite=False`
- NVENC encode
- Glare and Lens Distortion spliced into the scene's compositor

**Avoid:**
- compositor mask, ellipse and colour-balance nodes (grade in ffmpeg)
- nesting the scene's compositor group (renders black)
- Track To flips when the aim is exactly vertical, so keep aim points off the camera's plumb line

**Eevee previews differ from Cycles:**
- Glass reads as dark.
- Light falloff is flatter.
- Previews are for framing, move and focus, not for look.

## Examples
- `camera/examples/ramen_shots.json` and `camera/examples/repair_shots.json`: 35 mm push-in with a foreground->subject
  rack, 85 mm static handheld at f/1.8, 24 mm orbit, and a dolly zoom, written for two of AnyScene's reference sets
  (the object names are from those scenes).
