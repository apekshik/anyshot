"""camrig: cinematic camera rigs for the night-shop scenes (Blender 5.0). See README.md for the shots.json contract.

Rig per shot:  CR_<shot>_base (baked position) -> CR_<shot>_aim (Track To CR_<shot>_target) -> CR_<shot>_shake (noise)
               -> CR_<shot>_cam (lens/roll/DOF), plus CR_<shot>_focus (DOF focus object).
Moves are evaluated in Python and baked one key per frame, so every move type (eased keys, Catmull-Rom paths, orbits,
dolly zooms) behaves the same and motion blur sees smooth subframes; handheld is live Noise modifiers on the shake empty."""
import math

import bpy
from mathutils import Vector

SENSORS = {"s35": 24.89, "ff": 36.0}
ASPECTS = {"2.39": 2.39, "2.35": 2.35, "1.85": 1.85, "16:9": 16 / 9, "1.78": 16 / 9, "4:3": 4 / 3}
LENS_CHART = (18, 24, 35, 50, 85, 135)


# ------------------------------------------------------------------ scene lookups
def find(name):
    """Object by exact name, then prefix, then case-insensitive substring."""
    obs = bpy.data.objects
    if name in obs:
        return obs[name]
    for o in obs:
        if o.name.startswith(name):
            return o
    low = name.lower()
    for o in obs:
        if low in o.name.lower():
            return o
    raise KeyError(f"camrig: no object matches '{name}'")


def bounds(name_or_obj):
    """World-space (min, max, center) of an object and all its mesh descendants."""
    root = find(name_or_obj) if isinstance(name_or_obj, str) else name_or_obj
    pts = []
    for o in [root, *root.children_recursive]:
        if o.type in {"MESH", "CURVE", "FONT", "SURFACE", "META", "VOLUME"}:
            pts += [o.matrix_world @ Vector(c) for c in o.bound_box]
    if not pts:
        p = root.matrix_world.translation.copy()
        return p, p, p
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi, (lo + hi) / 2


def point(spec, offset=None):
    """[x,y,z] or an object name (bounds centre) -> Vector."""
    p = bounds(spec)[2] if isinstance(spec, (str, bpy.types.Object)) else Vector(spec)
    return p + Vector(offset) if offset else p


# ------------------------------------------------------------------ easing / interpolation
def ease(u, mode="inout"):
    u = min(1.0, max(0.0, u))
    if mode == "linear":
        return u
    if mode == "in":
        return 1 - math.cos(u * math.pi / 2)
    if mode == "out":
        return math.sin(u * math.pi / 2)
    return 0.5 - 0.5 * math.cos(u * math.pi)


def _segment(ts, t):
    if t <= ts[0]:
        return 0, 0.0
    for i in range(len(ts) - 1):
        if t <= ts[i + 1]:
            span = ts[i + 1] - ts[i]
            return i, (t - ts[i]) / span if span > 0 else 1.0
    return len(ts) - 2, 1.0


def path_at(ts, pts, t):
    """Through all points: linear for 2, Catmull-Rom (uniform per segment) for 3+. t is already eased."""
    if len(pts) == 1:
        return Vector(pts[0])
    i, u = _segment(ts, t)
    p1, p2 = Vector(pts[i]), Vector(pts[i + 1])
    if len(pts) == 2:
        return p1.lerp(p2, u)
    p0 = Vector(pts[i - 1]) if i > 0 else p1 + (p1 - p2)
    p3 = Vector(pts[i + 2]) if i + 2 < len(pts) else p2 + (p2 - p1)
    u2, u3 = u * u, u * u * u
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2 + (-p0 + 3 * p1 - 3 * p2 + p3) * u3)


def segment_at(ts, vals, t, mode):
    """Per-segment eased interpolation (holds + pulls): focus racks, lens zooms, f-stop, roll."""
    if len(vals) == 1:
        return vals[0]
    i, u = _segment(ts, t)
    e = ease(u, mode)
    a, b = vals[i], vals[i + 1]
    if isinstance(a, Vector):
        return a.lerp(b, e)
    return a + (b - a) * e


# ------------------------------------------------------------------ animation helpers (5.0 layered actions)
def _fc(id_, path, index=0):
    if id_.animation_data is None:
        id_.animation_data_create()
    ad = id_.animation_data
    if ad.action is None:
        ad.action = bpy.data.actions.new(f"{id_.name}_act")
    return ad.action.fcurve_ensure_for_datablock(id_, path, index=index)


def bake(id_, path, values, index=None, frame0=1):
    """values: list per frame (scalar or sequence). One LINEAR key per frame."""
    seq = isinstance(values[0], (list, tuple, Vector))
    idxs = range(len(values[0])) if seq else [index or 0]
    for j in idxs:
        fc = _fc(id_, path, j)
        fc.keyframe_points.clear()
        fc.keyframe_points.add(len(values))
        co = []
        for k, v in enumerate(values):
            co += [frame0 + k, v[j] if seq else v]
        fc.keyframe_points.foreach_set("co", co)
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
        fc.update()


def _noise(fc, scale, strength, phase, depth=0):
    m = fc.modifiers.new("NOISE")
    m.blend_type = "REPLACE"  # value + (noise - 0.5) * strength: centred, stacks additively
    m.scale, m.strength, m.phase, m.depth = scale, strength, phase, depth
    return m


def shake(obj, handheld, breathing, seed):
    """Layered noise on location + rotation of the shake empty. Amplitudes are peak-to-peak-ish at intensity 1."""
    obj.animation_data_clear()
    for path in ("location", "rotation_euler"):
        for i in range(3):
            fc = _fc(obj, path, i)
            fc.keyframe_points.insert(1, 0.0)
            ph = seed * 13.7 + i * 5.3 + (0 if path == "location" else 101)
            if path == "location":
                if breathing:
                    _noise(fc, 48, 0.014 * breathing, ph, depth=1)  # slow body sway (~2 s)
                if handheld:
                    _noise(fc, 11, 0.004 * handheld, ph + 31)  # step/hand bob
                    _noise(fc, 3.5, 0.0012 * handheld, ph + 57)  # fine jitter
            else:
                k = 0.6 if i < 2 else 0.35  # less roll than pan/tilt
                if breathing:
                    _noise(fc, 56, math.radians(0.7 * k) * breathing, ph, depth=1)
                if handheld:
                    _noise(fc, 13, math.radians(0.45 * k) * handheld, ph + 31)
                    _noise(fc, 4, math.radians(0.12 * k) * handheld, ph + 57)


# ------------------------------------------------------------------ handheld "walk" profile (opt-in)
def walk_settings(s):
    """Return the walk dict if the shot opts in with "handheld": {"profile": "walk", ...}, else None."""
    h = s.get("handheld")
    if isinstance(h, dict) and h.get("profile") == "walk":
        return h
    return None


def _spring(xs, fps, freq=0.9, zeta=0.62):
    """Second-order follower (semi-implicit Euler, 8 substeps/frame): lags behind moves and overshoots slightly on settle."""
    w = 2 * math.pi * freq
    y, v = xs[0].copy(), Vector((0, 0, 0))
    out, dt = [], 1.0 / fps / 8
    for x in xs:
        for _ in range(8):
            a = w * w * (x - y) - 2 * zeta * w * v
            v += a * dt
            y += v * dt
        out.append(y.copy())
    return out


def _smooth(vals, k):
    """Symmetric moving average (k frames each side), edge-clamped."""
    n = len(vals)
    return [sum(vals[min(n - 1, max(0, i + j))] for j in range(-k, k + 1)) / (2 * k + 1) for i in range(n)]


def walk_motion(ev, s, seed):
    """Per-frame (location xyz, rotation xyz) for the shake empty, plus a lagged aim path. All components <= ~1.9 Hz:
    footstep bob + alternating roll scaled by walking speed, low-frequency body sway (calmer when standing), aim lag and
    overshoot, and one or two small reframing corrections. Units: metres / radians, in the camera's local frame
    (x right, y up, z back)."""
    h = walk_settings(s)
    amt = float(h.get("amount", 1.0))
    fps = float(s.get("fps", 24))
    n = ev["n"]
    pos = ev["pos"]
    # walking intensity from the move's speed: 0 standing, 1 at >= walk_speed m/s, eased and smoothed so steps fade in/out
    vmax = float(h.get("walk_speed", 0.55))
    sp = [(pos[min(n - 1, i + 1)] - pos[max(0, i - 1)]).length * fps / (2 if 0 < i < n - 1 else 1) for i in range(n)]
    wv = [min(1.0, v / vmax) for v in sp]
    wv = _smooth([x * x * (3 - 2 * x) for x in wv], int(fps * 0.4))
    step_hz = float(h.get("step_hz", 1.8))
    rnd = __import__("random").Random(seed * 7919 + 17)
    ph = [rnd.uniform(0, 6.283) for _ in range(12)]
    loc, rot = [], []
    phi = 0.0
    # reframing corrections: slow drift off, quicker fix back (pan), at one or two moments
    ncorr = int(h.get("corrections", 2))
    corr_t = sorted(rnd.uniform(0.25, 0.85) for _ in range(ncorr))
    corr_sign = [rnd.choice((-1, 1)) for _ in range(ncorr)]
    for i in range(n):
        t = i / fps
        w = wv[i]
        phi += 2 * math.pi * step_hz / fps * (0.35 + 0.65 * w) if w > 0.02 else 0.0
        calm = 0.45 + 0.55 * w  # body sway is calmer when the operator stands
        sway = lambda f, p, a: a * math.sin(2 * math.pi * f * t + p)
        x = sway(0.23, ph[0], 0.016) + sway(0.47, ph[1], 0.008)
        y = sway(0.31, ph[2], 0.010) + sway(0.57, ph[3], 0.005)
        z = sway(0.19, ph[4], 0.012) + sway(0.41, ph[5], 0.006)
        bob = 0.010 * w * (0.5 - 0.5 * math.cos(phi))  # dip once per step (heel strike), ~1 cm
        loc.append(Vector((x * calm + 0.004 * w * math.sin(phi / 2), y * calm - bob, z * calm)) * amt)
        tilt = math.radians(sway(0.29, ph[6], 0.45) + sway(0.53, ph[7], 0.25)) * calm + math.radians(0.12) * w * math.cos(phi)
        pan = math.radians(sway(0.21, ph[8], 0.5) + sway(0.44, ph[9], 0.25)) * calm
        roll = math.radians(0.3) * w * math.sin(phi / 2) + math.radians(sway(0.17, ph[10], 0.25)) * calm
        for ct, cs in zip(corr_t, corr_sign):
            u = i / max(1, n - 1)
            d0, d1 = ct - 0.09, ct  # drift over ~9% of the shot, fix in ~0.35 s
            fix = 0.35 * fps / max(1, n - 1)
            if d0 <= u < d1:
                k = (u - d0) / (d1 - d0)
                pan += cs * math.radians(0.6) * k * k * (3 - 2 * k)
            elif d1 <= u < d1 + fix:
                k = (u - d1) / fix
                pan += cs * math.radians(0.6) * (1 - k * k * (3 - 2 * k))
        rot.append(Vector((tilt, pan, roll)) * amt)
    aim = _spring(ev["aim"], fps, float(h.get("aim_lag_hz", 0.9)), float(h.get("aim_damping", 0.62)))
    return loc, rot, aim


# ------------------------------------------------------------------ shot evaluation
def merged(defaults, shot):
    s = dict(defaults)
    s.update(shot)
    return s


def evaluate(s):
    """Per-frame lists: pos, aim, lens, roll, focus (Vector or None), fstop."""
    n = int(s.get("frames", 120))
    mode = s.get("ease", "inout")
    move = s.get("move", "static")
    keys = sorted(s.get("keys", []), key=lambda k: k.get("t", 0))
    lens0 = s.get("lens", 35)
    T = [i / max(1, n - 1) for i in range(n)]

    def kpt(k, field):
        if field == "aim" and "aim_obj" in k:
            return point(k["aim_obj"], k.get("aim_offset"))
        if field == "pos" and "pos_obj" in k:
            return point(k["pos_obj"], k.get("pos_offset"))
        return Vector(k[field]) if field in k else None

    if move == "orbit":
        o = s["orbit"]
        c = point(o.get("center_obj") or o["center"], o.get("center_offset"))
        r, a0, a1 = o["radius"], o.get("start_deg", 0), o.get("end_deg", 90)
        h = o.get("height", c.z)
        pos = []
        for t in T:
            a = math.radians(a0 + (a1 - a0) * ease(t, mode))
            pos.append(Vector((c.x + r * math.cos(a), c.y + r * math.sin(a), h)))
        aim_pt = point(o["aim_obj"], o.get("aim_offset")) if "aim_obj" in o else (Vector(o["aim"]) if "aim" in o else c)
        aim = [aim_pt.copy() for _ in T]
    else:
        if not keys:
            raise ValueError(f"shot {s['name']}: move '{move}' needs keys")
        ts = [k.get("t", 0) for k in keys]
        P = [kpt(k, "pos") for k in keys]
        A = [kpt(k, "aim") for k in keys]
        # keys may omit pos/aim to hold the previous value
        for L in (P, A):
            for i in range(len(L)):
                if L[i] is None:
                    L[i] = L[i - 1] if i else next(x for x in L if x is not None)
        pos = [path_at(ts, P, ease(t, mode)) for t in T]
        aim = [path_at(ts, A, ease(t, mode)) for t in T]

    # lens
    lkeys = [(k.get("t", 0), k["lens"]) for k in keys if "lens" in k]
    if move == "dolly_zoom":
        subj = point(s["subject"]) if "subject" in s else aim[0]
        l0 = lkeys[0][1] if lkeys else lens0
        d0 = (subj - pos[0]).length
        lens = [l0 * (subj - p).length / d0 for p in pos]
    elif lkeys:
        lens = [segment_at([a for a, _ in lkeys], [b for _, b in lkeys], t, mode) for t in T]
    else:
        lens = [lens0] * n
    rkeys = [(k.get("t", 0), k["roll"]) for k in keys if "roll" in k]
    roll = [segment_at([a for a, _ in rkeys], [b for _, b in rkeys], t, mode) for t in T] if rkeys else [0.0] * n

    # focus
    f = s.get("focus", {"mode": "track_aim"})
    fm = f.get("mode", "point")
    if fm == "none":
        focus = None
    elif fm == "rack":
        fk = sorted(f["keys"], key=lambda k: k.get("t", 0))
        fts = [k.get("t", 0) for k in fk]
        fps = [point(k["obj"], k.get("offset")) if "obj" in k else Vector(k["pos"]) for k in fk]
        focus = [segment_at(fts, fps, t, f.get("ease", "inout")) for t in T]
    elif fm == "track":
        focus = "track"
    elif fm == "point":
        p = Vector(f["pos"]) if "pos" in f else point(f["obj"], f.get("offset"))
        focus = [p] * n
    else:  # default: focus where the camera aims
        focus = list(aim)

    if "fstop_keys" in s:
        fk = sorted(s["fstop_keys"], key=lambda k: k["t"])
        fstop = [segment_at([k["t"] for k in fk], [k["v"] for k in fk], t, mode) for t in T]
    else:
        fstop = [s.get("fstop", 2.8)] * n
    return dict(n=n, pos=pos, aim=aim, lens=lens, roll=roll, focus=focus, fstop=fstop)


# ------------------------------------------------------------------ rig build
def _empty(name, coll, size=0.1, shape="PLAIN_AXES"):
    o = bpy.data.objects.get(name)
    if o is None:
        o = bpy.data.objects.new(name, None)
        coll.objects.link(o)
    o.empty_display_size, o.empty_display_type = size, shape
    return o


def clear_rig(name):
    for o in [o for o in bpy.data.objects if o.name.startswith(f"CR_{name}_")]:
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.cameras.remove(data)
    c = bpy.data.collections.get(f"CamRig_{name}")
    if c:
        bpy.data.collections.remove(c)


def build(s, seed=0):
    """Create the rig for merged shot dict s. Returns the camera object."""
    name = s["name"]
    clear_rig(name)
    sc = bpy.context.scene
    root = bpy.data.collections.get("camrig") or bpy.data.collections.new("camrig")
    if root.name not in sc.collection.children:
        sc.collection.children.link(root)
    coll = bpy.data.collections.new(f"CamRig_{name}")
    root.children.link(coll)

    ev = evaluate(s)
    walk = None
    if walk_settings(s):
        wl, wr, ev["aim"] = walk_motion(ev, s, seed + 1)
        if ev["focus"] is not None and ev["focus"] != "track" and s.get("focus", {}).get("mode", "track_aim") == "track_aim":
            ev["focus"] = list(ev["aim"])
        walk = (wl, wr)
    base = _empty(f"CR_{name}_base", coll, 0.15, "CUBE")
    aim = _empty(f"CR_{name}_aim", coll, 0.1, "ARROWS")
    shk = _empty(f"CR_{name}_shake", coll, 0.05)
    tgt = _empty(f"CR_{name}_target", coll, 0.08, "SPHERE")
    foc = _empty(f"CR_{name}_focus", coll, 0.05, "CIRCLE")
    cd = bpy.data.cameras.new(f"CR_{name}_cam")
    cam = bpy.data.objects.new(f"CR_{name}_cam", cd)
    coll.objects.link(cam)
    aim.parent, shk.parent, cam.parent = base, aim, shk
    con = aim.constraints.new("TRACK_TO")
    con.target, con.track_axis, con.up_axis = tgt, "TRACK_NEGATIVE_Z", "UP_Y"

    bake(base, "location", [tuple(p) for p in ev["pos"]])
    bake(tgt, "location", [tuple(p) for p in ev["aim"]])
    bake(cam, "rotation_euler", [math.radians(r) for r in ev["roll"]], index=2)

    sens = s.get("sensor", "s35")
    cd.sensor_width = SENSORS.get(sens, sens) if isinstance(sens, str) else float(sens)
    cd.sensor_fit = "HORIZONTAL"
    cd.clip_start, cd.clip_end = 0.02, 300
    bake(cd, "lens", ev["lens"])
    dof = cd.dof
    if ev["focus"] is None:
        dof.use_dof = False
    else:
        dof.use_dof = True
        dof.focus_object = foc
        dof.aperture_blades = int(s.get("blades", 0))
        dof.aperture_ratio = float(s.get("ratio", 1.0))
        bake(cd, "dof.aperture_fstop", ev["fstop"])
        if ev["focus"] == "track":
            f = s["focus"]
            ob = find(f["obj"])
            c = point(ob, f.get("offset"))
            foc.parent = ob
            foc.matrix_parent_inverse = ob.matrix_world.inverted()
            foc.location = c
        else:
            bake(foc, "location", [tuple(p) for p in ev["focus"]])

    if walk:
        shk.animation_data_clear()
        bake(shk, "location", [tuple(v) for v in walk[0]])
        bake(shk, "rotation_euler", [tuple(v) for v in walk[1]])
    else:
        hh = float(s.get("handheld", 0))
        br = float(s.get("breathing", hh))
        if hh or br:
            shake(shk, hh, br, seed + 1)
    cam["camrig"] = {"lens0": ev["lens"][0], "frames": ev["n"]}
    return cam


# ------------------------------------------------------------------ render setup
def eevee_id():
    ids = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    return "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in ids else "BLENDER_EEVEE"


def res_for(s):
    w, h = s.get("res", [1920, 1080])
    a = s.get("aspect", "16:9")
    a = ASPECTS.get(str(a), None) if not isinstance(a, (int, float)) else float(a)
    if a and abs(a - w / h) > 0.01:
        h = int(round(w / a / 2) * 2)
    return w, h


def setup_render(s, cam, mode="final"):
    sc = bpy.context.scene
    r = sc.render
    sc.camera = cam
    sc.frame_start, sc.frame_end, sc.frame_step = 1, int(s.get("frames", 120)), 1
    r.fps = int(s.get("fps", 24))
    r.resolution_x, r.resolution_y = res_for(s)
    r.resolution_percentage = 100
    r.pixel_aspect_x = r.pixel_aspect_y = 1
    r.use_border = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.color_depth = "16" if (mode == "final" and s.get("png16", True)) else "8"
    r.use_overwrite = False
    r.use_placeholder = False
    sh = float(s.get("shutter", 180))
    r.use_motion_blur = sh > 0
    r.motion_blur_shutter = sh / 360.0
    _fix_exposure(sc, s.get("exposure", 0.0))
    if mode == "final":
        r.engine = "CYCLES"
        sc.cycles.samples = int(s.get("samples", sc.cycles.samples))
        r.use_persistent_data = True
        r.use_stamp = False
    else:
        r.engine = s.get("_engine", eevee_id())
        r.resolution_percentage = s.get("_pct", 50)
        if r.engine.startswith("BLENDER_EEVEE"):
            sc.eevee.taa_render_samples = 16
            r.use_motion_blur = False
        elif r.engine == "CYCLES":
            sc.cycles.samples = 16
            sc.cycles.use_denoising = True
        stamp(sc)
    if sc.name not in _COMP_BASE:
        _COMP_BASE[sc.name] = sc.compositing_node_group
    sc.compositing_node_group = _COMP_BASE[sc.name]
    if s.get("lens_fx"):
        lens_fx(sc, s["lens_fx"])


_COMP_BASE = {}


_EXPOSURE_BASE = {}


def _fix_exposure(sc, offset):
    """Scenes ship with a keyed exposure drift for their old clip; freeze it at its frame-1 value (+ shot offset)."""
    vs = sc.view_settings
    if sc.name not in _EXPOSURE_BASE:
        ad = sc.animation_data
        if ad and ad.action:
            sc.frame_set(1)
            base = vs.exposure
            for fc in _fcurves(ad):
                if fc.data_path == "view_settings.exposure":
                    _fcurves(ad).remove(fc)
                    break
        else:
            base = vs.exposure
        _EXPOSURE_BASE[sc.name] = base
    vs.exposure = _EXPOSURE_BASE[sc.name] + offset


def _fcurves(ad):
    act = ad.action
    if hasattr(act, "fcurves"):
        return act.fcurves
    from bpy_extras import anim_utils
    return anim_utils.action_get_channelbag_for_slot(act, ad.action_slot).fcurves


def stamp(sc):
    r = sc.render
    r.use_stamp = True
    for k in ("date", "time", "render_time", "filename", "scene", "memory", "hostname", "marker", "frame_range",
              "sequencer_strip", "camera"):
        if hasattr(r, f"use_stamp_{k}"):
            setattr(r, f"use_stamp_{k}", False)
    r.use_stamp_frame = True
    r.use_stamp_lens = True
    r.use_stamp_note = True
    r.stamp_font_size = 14
    r.stamp_background = (0, 0, 0, 0.6)


def stamp_note(sc, depsgraph=None):
    cam = sc.camera
    if cam is None or not cam.name.startswith("CR_"):
        return
    ce = cam.evaluated_get(depsgraph) if depsgraph else cam
    cd = ce.data
    mw = ce.matrix_world
    fwd = -(mw.to_3x3() @ Vector((0, 0, 1))).normalized()
    if cd.dof.use_dof and cd.dof.focus_object:
        fo = cd.dof.focus_object.evaluated_get(depsgraph) if depsgraph else cd.dof.focus_object
        dist = (fo.matrix_world.translation - mw.translation).dot(fwd)
        dof = f"f/{cd.dof.aperture_fstop:.1f}  focus {dist:.2f} m"
    else:
        dof = "no DOF"
    shot = cam.name[3:-4]
    sc.render.stamp_note_text = f"{shot}  {cd.lens:.0f}mm  {dof}"


def _stamp_handler(scene, depsgraph=None):
    stamp_note(scene, depsgraph)


def install_handler():
    h = bpy.app.handlers.frame_change_post
    for f in list(h):
        if getattr(f, "__name__", "") == "_stamp_handler":
            h.remove(f)
    h.append(_stamp_handler)


def lens_fx(sc, fx):
    """Glare + lens distortion/dispersion via the 5.0 node-group compositor, appended after the scene's own tree."""
    # A Render Layers node nested inside a group renders black in 5.0, so copy the scene's tree and splice in before
    # its Group Output instead.
    old = sc.compositing_node_group
    out = None
    if old is not None:
        t = old.copy()
        t.name = "camrig_fx"
        out = next((n for n in t.nodes if n.bl_idname == "NodeGroupOutput"), None)
    if out is None or not out.inputs[0].links:
        t = bpy.data.node_groups.new("camrig_fx", "CompositorNodeTree")
        t.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        img = t.nodes.new("CompositorNodeRLayers").outputs["Image"]
        out = t.nodes.new("NodeGroupOutput")
    else:
        lk = out.inputs[0].links[0]
        img = lk.from_socket
        t.links.remove(lk)
    N, L = t.nodes, t.links.new
    if fx.get("glare"):
        g = N.new("CompositorNodeGlare")
        g.inputs["Type"].default_value = {"fog": "Fog Glow", "bloom": "Bloom", "streaks": "Streaks"}.get(fx["glare"], "Bloom")
        g.inputs["Threshold"].default_value = fx.get("glare_threshold", 1.0)
        g.inputs["Strength"].default_value = fx.get("glare_strength", 0.15)
        L(img, g.inputs["Image"])
        img = g.outputs["Image"]
    if fx.get("distortion") or fx.get("dispersion"):
        ld = N.new("CompositorNodeLensdist")
        ld.inputs["Distortion"].default_value = fx.get("distortion", 0.0)
        ld.inputs["Dispersion"].default_value = fx.get("dispersion", 0.0)
        L(img, ld.inputs["Image"])
        img = ld.outputs["Image"]
    L(img, out.inputs[0])
    sc.compositing_node_group = t
    sc.render.use_compositing = True
