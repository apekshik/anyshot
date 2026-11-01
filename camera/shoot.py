"""camrig CLI. Run through pipeline/gpu_run.sh:
  blender -b <scene.blend> --python camera/shoot.py -- --shots <shots.json> --out <abs dir>
      [--only a,b] [--preview | --framing | --lenschart NAME | --still N] [--frames a-b] [--save x.blend] [--no-encode]
      [--engine eevee|cycles|workbench] [--step 2]
  blender -b <scene.blend> --python camera/shoot.py -- --list <pattern>
See README.md."""
import argparse
import json
import os
import subprocess
import sys
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import camrig  # noqa: E402

FF = os.environ.get("FFMPEG", "ffmpeg")


def args():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--shots")
    p.add_argument("--out")
    p.add_argument("--only", default="")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--framing", action="store_true")
    p.add_argument("--lenschart")
    p.add_argument("--still", type=int)
    p.add_argument("--frames")
    p.add_argument("--save")
    p.add_argument("--no-encode", action="store_true")
    p.add_argument("--engine", default="eevee")
    p.add_argument("--step", type=int, default=1)
    p.add_argument("--list")
    return p.parse_args(a)


def ff(*cmd):
    r = subprocess.run([FF, "-v", "error", "-y", *cmd], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-800:])


def encode(files, fps, out):
    """files: ordered frame paths (any numbering) -> H.264 mp4 via concat list (no glob)."""
    lst = out + ".txt"
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{p.replace(os.sep, '/')}'\nduration {1 / fps:.6f}\n")
        f.write(f"file '{files[-1].replace(os.sep, '/')}'\n")
    common = ["-f", "concat", "-safe", "0", "-i", lst, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
              "-r", str(fps), "-movflags", "+faststart"]
    try:
        ff(*common, "-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "18", "-b:v", "0", out)
    except RuntimeError:
        ff(*common, "-c:v", "h264_mf", "-b:v", "20M", out)
    os.remove(lst)
    print("camrig: encoded", out)


def sheet(files, cols, out, width=640):
    """Equal-size tiles in a grid (pads with black)."""
    n = len(files)
    rows = (n + cols - 1) // cols
    ins = []
    for p in files:
        ins += ["-i", p]
    pad = rows * cols - n
    for _ in range(pad):
        ins += ["-i", files[0]]
    flt = "".join(f"[{i}:v]scale={width}:-2{',drawbox=c=black:t=fill' if i >= n else ''}[v{i}];" for i in range(rows * cols))
    lay = "|".join(("+".join(["w0"] * (i % cols)) or "0") + "_" + ("+".join(["h0"] * (i // cols)) or "0")
                   for i in range(rows * cols))
    flt += "".join(f"[v{i}]" for i in range(rows * cols)) + f"xstack=inputs={rows * cols}:layout={lay}"
    if rows * cols == 1:
        flt = "[0:v]scale=%d:-2" % width
    ff(*ins, "-filter_complex", flt, "-frames:v", "1", out)
    print("camrig: sheet", out)


def render_still(path):
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    a = args()
    if a.list:
        for o in sorted(bpy.data.objects, key=lambda o: o.name):
            if a.list.lower() in o.name.lower():
                lo, hi, c = camrig.bounds(o)
                sz = hi - lo
                print(f"  {o.name:40s} center ({c.x:.3f}, {c.y:.3f}, {c.z:.3f})  size ({sz.x:.2f}, {sz.y:.2f}, {sz.z:.2f})")
        return
    cfg = json.load(open(a.shots, encoding="utf-8"))
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    only = {x for x in a.only.split(",") if x}
    shots = [camrig.merged(cfg.get("defaults", {}), s) for s in cfg["shots"]]
    if a.lenschart:
        only = {a.lenschart}
    if only:
        missing = only - {s["name"] for s in shots}
        if missing:
            raise SystemExit(f"camrig: unknown shots {missing}")
        shots = [s for s in shots if s["name"] in only]
    sc = bpy.context.scene
    camrig.install_handler()
    engine = {"eevee": camrig.eevee_id(), "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"}[a.engine]

    cams = {}
    for i, s in enumerate(shots):
        cams[s["name"]] = camrig.build(s, seed=i * 7)
        print(f"camrig: rig {s['name']} ({s.get('move', 'static')}, {s.get('frames', 120)} f)")
    if a.save:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(a.save), copy=True)
        print("camrig: saved", a.save)

    framing_tiles = []
    for s in shots:
        name, cam = s["name"], cams[s["name"]]
        d = os.path.join(out, name)
        n = int(s.get("frames", 120))
        if a.framing or a.lenschart:
            s2 = dict(s, _engine=engine, _pct=100, res=[640, int(640 / (camrig.res_for(s)[0] / camrig.res_for(s)[1]))])
            camrig.setup_render(s2, cam, mode="preview")
            if a.framing:
                for tag, f in (("t0", 1), ("t5", (n + 1) // 2), ("t10", n)):
                    sc.frame_set(f)
                    camrig.stamp_note(sc, bpy.context.evaluated_depsgraph_get())
                    p = os.path.join(d, "framing", tag + ".png")
                    render_still(p)
                    framing_tiles.append(p)
            else:
                sc.frame_set(1)
                tiles = []
                for mm in camrig.LENS_CHART:
                    for fc_owner in (cam.data,):
                        if fc_owner.animation_data:
                            fc_owner.animation_data.action = None
                    cam.data.lens = mm
                    camrig.stamp_note(sc, bpy.context.evaluated_depsgraph_get())
                    p = os.path.join(d, "lenschart", f"{mm:03d}.png")
                    render_still(p)
                    tiles.append(p)
                sheet(tiles, 3, os.path.join(out, f"{name}_lenschart.png"))
            continue
        if a.preview:
            s2 = dict(s, _engine=engine, _pct=50)
            camrig.setup_render(s2, cam, mode="preview")
            sc.frame_step = max(1, a.step)
            sc.render.filepath = os.path.join(d, "preview", "")
            t0 = time.time()
            bpy.ops.render.render(animation=True)
            files = [os.path.join(d, "preview", f"{f:04d}.png") for f in range(1, n + 1, sc.frame_step)]
            files = [p for p in files if os.path.exists(p)]
            print(f"camrig: preview {name} {len(files)} frames in {time.time() - t0:.0f}s")
            if not a.no_encode and files:
                encode(files, s.get("fps", 24) / sc.frame_step, os.path.join(d, "preview.mp4"))
            continue
        camrig.setup_render(s, cam, mode="final")
        if a.still:
            sc.frame_set(a.still)
            sc.render.use_overwrite = True
            t0 = time.time()
            render_still(os.path.join(d, f"still_{a.still:04d}.png"))
            print(f"camrig: still {name} f{a.still} in {time.time() - t0:.0f}s")
            continue
        f0, f1 = 1, n
        if a.frames:
            f0, f1 = (int(x) for x in a.frames.split("-"))
            f1 = min(f1, n)
        sc.frame_start, sc.frame_end = f0, f1
        sc.render.filepath = os.path.join(d, "frames", "")
        t0 = time.time()
        bpy.ops.render.render(animation=True)
        print(f"camrig: final {name} frames {f0}-{f1} in {time.time() - t0:.0f}s")
        files = [os.path.join(d, "frames", f"{f:04d}.png") for f in range(1, n + 1)]
        if not a.no_encode and all(os.path.exists(p) for p in files):
            encode(files, s.get("fps", 24), os.path.join(d, f"{name}.mp4"))
            sheet([files[0], files[n // 2], files[-1]], 3, os.path.join(d, "contact.png"))
        elif not a.no_encode:
            print(f"camrig: {name} incomplete ({sum(os.path.exists(p) for p in files)}/{n}); mp4 after the last chunk")
    if framing_tiles:
        sheet(framing_tiles, 3, os.path.join(out, "_framing.png"), width=480)


main()
