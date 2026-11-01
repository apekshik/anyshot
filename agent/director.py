"""The shots agent: a Claude DP that shoots a 3D set (see agent/SHOTS.md for its instructions).

The set is either an uploaded scene unpacked into a workspace of its own (input/scene/) or an AnyScene build's own
workspace (/data/anyscene/builder/<build_id>: the finished out/scene.blend, the builder's scripts, NOTES.md and its
scouting cameras). Each order gets a job folder out/shots/<job_id>/ with request.json; the agent blocks every shot
with quick previews, then renders finals: stills on this worker's GPU, or video frames on a render farm app
(farm/app.py) through the farm_render tool.
"""

import asyncio
import hashlib
import io
import json
import os
import re
import urllib.request
import zipfile

from claude_agent_sdk import tool

import harness
from harness import Agent

# The render farm app (farm/app.py deployed on your own account), e.g. "<your-fal-username>/blender-farm". Read when a
# farm call is made, so a fal secret set after deploy applies. A request's `farm_app` overrides it for that order.
# Unset: farm_render fails and the agent renders the frames on its own GPU instead (slower, same result).
FARM_APP_ENV = "ANYSHOT_FARM_APP"
# Frames are spread over farm workers in chunks of at least FARM_MIN_CHUNK frames, up to FARM_WORKERS at once (default
# 100, the farm app's max_concurrency). Every chunk pays a worker boot, the scene download and Blender loading the set
# (~2 min together), so small chunks spend most of their GPU time on setup: at 5 frames a chunk that was ~75% of the
# bill. 24 frames a chunk keeps the wall time to a few minutes while most of each worker's time goes to rendering.
FARM_PARALLEL = int(os.environ.get("FARM_WORKERS", "100"))
FARM_MIN_CHUNK = int(os.environ.get("FARM_MIN_CHUNK", "24"))
# The GPU-forcing startup script the app writes on its worker (agent/blender_gpu.py); also shipped to the farm
BLENDER_GPU_PATH = "/opt/anyshot/blender_gpu.py"
FARM_APP_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def farm_app(override=None):
    app = (override or os.environ.get(FARM_APP_ENV, "")).strip().strip("/")
    if app and not FARM_APP_RE.match(app):
        raise ValueError(f"bad farm app id {app!r}: expected <owner>/<app>")
    return app


def toolkit_files():
    """What every workspace gets, as {relative path: text}: the agent's instructions (.anyshot/SHOTS.md), the camrig
    shot rig (camera/) and the GPU queue helpers (pipeline/). Collected at deploy time so the fal app carries it."""
    files = {".anyshot/SHOTS.md": open(os.path.join(HERE, "SHOTS.md"), encoding="utf-8").read()}
    for rel in ("pipeline/env.sh", "pipeline/gpu_run.sh"):
        files[rel] = open(os.path.join(REPO, rel), encoding="utf-8").read()
    for root, dirs, names in os.walk(os.path.join(REPO, "camera")):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for n in names:
            if n.endswith((".py", ".md", ".json")):
                path = os.path.join(root, n)
                files[os.path.relpath(path, REPO).replace(os.sep, "/")] = open(path, encoding="utf-8").read()
    return files


class ShotsDirector(Agent):
    PROMPT_PATH = ".anyshot/SHOTS.md"

    def __init__(self, work, scene, job_id, request, resume=False, farm=None, **kw):
        super().__init__(work, out_rel=os.path.join("out", "shots", job_id), **kw)
        self.scene, self.job_id, self.request, self.farm_override = scene, job_id, request, farm
        self.job = os.path.relpath(self.out, work).replace(os.sep, "/")
        with open(os.path.join(self.out, "request.json"), "w", encoding="utf-8") as f:
            json.dump(request, f, indent=1)
        kind = (request.get("source") or {}).get("kind", "build")
        if kind == "upload":
            self.ASK = (f"Shoot the order in {self.job}/request.json. The set is an uploaded scene in input/scene/ (not "
                        f"an AnyScene build). Start by reading the request, camera/README.md and, if an earlier order "
                        f"left one, scenes/{scene}/NOTES.md.")
        else:
            self.ASK = (f"Shoot the order in {self.job}/request.json on the built scene. Start by reading "
                        f"scenes/{scene}/NOTES.md, camera/README.md and the request.")
        if resume:
            self.ASK += (f" This order was cut off (its request was retried) and is resuming in the same job folder: "
                         f"read {self.job}/builder.log and {self.job}/events.jsonl and look at what is already in "
                         f"{self.job}/ first. Don't redo finished work; farm_render skips frames that already exist.")
        self.uploads = {}

    def tools(self):
        d = self

        @tool("farm_render", "Render every frame of the named video shots on the render farm (up to 100 GPUs). "
                             "Reads the cameras from <job>/shots.json and the set from <job>/scene.blend if it exists, "
                             "else out/scene.blend. Frames land in <job>/frames/<shot>/frames/####.png. Returns per-shot "
                             "frame counts and timing. Already-rendered frames are skipped, so it can be called again "
                             "to fill gaps or after changing one shot.",
              {"type": "object", "properties": {"shots": {"type": "array", "items": {"type": "string"}}},
               "required": ["shots"]})
        async def farm_render(args):
            try:
                text = await d.farm(args["shots"])
                return {"content": [{"type": "text", "text": text}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"farm_render failed: {type(e).__name__}: {e}"}],
                        "is_error": True}

        return super().tools() + [farm_render]

    async def farm(self, names):
        import fal_client

        app = farm_app(self.farm_override)
        if not app:
            raise RuntimeError(f"no render farm configured ({FARM_APP_ENV} is not set): render the frames on this GPU")
        shots_path = os.path.join(self.out, "shots.json")
        cfg = json.load(open(shots_path, encoding="utf-8"))
        defs = {s["name"]: s for s in cfg["shots"]}
        unknown = [n for n in names if n not in defs]
        if unknown:
            raise ValueError(f"not in {self.job}/shots.json: {unknown}")
        blend = os.path.join(self.out, "scene.blend")
        if not os.path.exists(blend):
            blend = os.path.join(self.work, "out", "scene.blend")
        if not os.path.exists(blend):
            raise FileNotFoundError(f"no set to send: save the packed copy as {self.job}/scene.blend first")

        # bundle: the set, the camera rig, these shots and the GPU script; the farm caches it by content hash
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.write(blend, "scene.blend")
            z.write(shots_path, "shots.json")
            for n in ("camrig.py", "shoot.py"):
                z.write(os.path.join(self.work, "camera", n), f"camera/{n}")
            z.write(BLENDER_GPU_PATH, "blender_gpu.py")
        data = buf.getvalue()
        bundle_id = hashlib.sha256(data).hexdigest()[:16]
        if bundle_id not in self.uploads:
            path = os.path.join(self.out, f"bundle_{bundle_id}.zip")
            with open(path, "wb") as f:
                f.write(data)
            self.uploads[bundle_id] = await asyncio.to_thread(self.upload, path)
            os.remove(path)
        url = self.uploads[bundle_id]

        frames_dir = os.path.join(self.out, "frames")
        total = sum(int(defs[n].get("frames", cfg.get("defaults", {}).get("frames", 120))) for n in names)
        chunk = max(FARM_MIN_CHUNK, -(-total // FARM_PARALLEL))
        jobs = []
        for name in names:
            n = int(defs[name].get("frames", cfg.get("defaults", {}).get("frames", 120)))
            have = {f for f in range(1, n + 1) if os.path.exists(os.path.join(frames_dir, name, "frames", f"{f:04d}.png"))}
            for a in range(1, n + 1, chunk):
                b = min(a + chunk - 1, n)
                if all(f in have for f in range(a, b + 1)):
                    continue
                args = ["{bundle}/scene.blend", "--python", "{bundle}/blender_gpu.py", "--python",
                        "{bundle}/camera/shoot.py", "--", "--shots", "{bundle}/shots.json", "--out", "{out}",
                        "--only", name, "--frames", f"{a}-{b}", "--no-encode"]
                jobs.append((name, a, b, {"bundle_url": url, "bundle_id": bundle_id, "blender_args": args}))
        if not jobs:
            return "every frame is already rendered"
        self.emit("farm", "running", f"render farm: {len(jobs)} chunks of up to {chunk} frames on up to {FARM_PARALLEL} GPUs for "
                                     f"{', '.join(names)}", bundle_url=url, bundle_id=bundle_id)

        client = fal_client.AsyncClient(key=os.environ.get("FAL_SERVERLESS_KEY") or os.environ.get("FAL_KEY"))
        sem = asyncio.Semaphore(FARM_PARALLEL)
        done, failed, gpu_s = 0, [], 0.0

        async def one(job):
            nonlocal done, gpu_s
            name, a, b, payload = job
            err = None
            async with sem:
                for attempt in range(3):
                    try:
                        res = await client.subscribe(app, arguments=payload)
                        raw = await asyncio.to_thread(
                            lambda: urllib.request.urlopen(res["frames_url"], timeout=600).read())
                        with zipfile.ZipFile(io.BytesIO(raw)) as z:
                            for member in z.namelist():  # <shot>/frames/####.png
                                dest = os.path.normpath(os.path.join(frames_dir, member))
                                if not dest.startswith(os.path.normpath(frames_dir) + os.sep):
                                    continue
                                os.makedirs(os.path.dirname(dest), exist_ok=True)
                                with open(dest, "wb") as f:
                                    f.write(z.read(member))
                        done += 1
                        gpu_s += res.get("render_seconds", 0)
                        self.emit("farm", "running", f"{name} frames {a}-{b} in ({done} of {len(jobs)} chunks)",
                                  progress=round(done / len(jobs), 3), bundle_s=res.get("bundle_seconds"),
                                  blender_s=res.get("render_seconds"))
                        return
                    except Exception as e:
                        err = f"{name} {a}-{b}: {type(e).__name__}: {str(e)[:200]}"
                failed.append(err)

        await asyncio.gather(*(one(j) for j in jobs))
        counts = {n: len([f for f in os.listdir(os.path.join(frames_dir, n, "frames"))])
                  if os.path.isdir(os.path.join(frames_dir, n, "frames")) else 0 for n in names}
        summary = (f"{done} of {len(jobs)} chunks rendered ({gpu_s / 3600:.2f} GPU-h). Frames per shot: {counts}."
                   + (f" Failed (call again to retry): {failed}" if failed else ""))
        self.emit("farm", "done" if not failed else "running", summary)
        return summary

    def format_prompt(self, text):
        return text.replace("{job}", self.job).replace("{scene}", self.scene)

    def missing(self):
        mode = self.request.get("mode", "images")
        # an order given only as a prompt has as many shots as the agent planned (at least one)
        names = [s.get("name") or f"shot_{i + 1}" for i, s in enumerate(self.request.get("shots", []))] or ["planned"]
        sub, ext = ("stills", ".png") if mode == "images" else ("video", ".mp4")
        have = [f for f in os.listdir(os.path.join(self.out, sub))] if os.path.isdir(os.path.join(self.out, sub)) else []
        miss = []
        if len([f for f in have if f.endswith(ext)]) < len(names):
            miss.append(f"{self.job}/{sub} has {len(have)} of {len(names)} {ext} files")
        if not os.path.exists(os.path.join(self.out, "result.json")):
            miss.append(f"{self.job}/result.json")
        return miss


# ---------------------------------------------------------------- workspace and inputs
def write_toolkit(work, toolkit):
    """Refresh the shared tools in a workspace (never a scene's own scripts or outputs)."""
    for rel, text in toolkit.items():
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if rel.endswith(".sh"):
            os.chmod(path, 0o755)


def build_scene_name(work):
    """The scene folder of an AnyScene build, checking it has something to shoot."""
    scenes = os.path.join(work, "scenes")
    if not os.path.isdir(scenes):
        raise ValueError("no such build")
    scene = next((d for d in sorted(os.listdir(scenes))), "scene")
    if not os.path.exists(os.path.join(work, "out", "scene.blend")) \
            and not os.path.exists(os.path.join(scenes, scene, "scene.blend")):
        # a build still running (or cut off) has no packed final yet: the agent shoots its working scene
        raise ValueError("the build has no scene file yet")
    return scene


def _safe_extract(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        for m in z.infolist():
            name = m.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or ":" in name.split("/")[0] or "__MACOSX" in name:
                continue
            target = os.path.join(dest, *name.split("/"))
            if m.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                while chunk := src.read(8 << 20):
                    out.write(chunk)


def fetch_scene(work, scene_url):
    """Download an uploaded scene once into <work>/input/scene/: a .blend as scene.blend, a .zip unpacked as it is.
    Concurrent orders on the same upload race harmlessly (each unpacks into its own folder; the first rename wins)."""
    dest = os.path.join(work, "input", "scene")
    if os.path.isdir(dest):
        return dest
    tmp = os.path.join(work, "input", f"_dl_{os.getpid()}_{os.urandom(3).hex()}")
    os.makedirs(tmp)
    raw = os.path.join(tmp, "download")
    with urllib.request.urlopen(scene_url, timeout=600) as r, open(raw, "wb") as f:
        while chunk := r.read(8 << 20):
            f.write(chunk)
    with open(raw, "rb") as f:
        head = f.read(8)
    unpacked = os.path.join(tmp, "scene")
    os.makedirs(unpacked)
    if head.startswith(b"PK\x03\x04"):
        _safe_extract(raw, unpacked)
    elif head.startswith((b"BLENDER", b"\x28\xb5\x2f\xfd", b"\x1f\x8b")):  # plain, zstd- or gzip-compressed .blend
        name = os.path.basename(scene_url.split("?", 1)[0])
        name = name if name.lower().endswith(".blend") and name.replace(".", "").replace("_", "").replace("-", "").isalnum() \
            else "scene.blend"
        os.replace(raw, os.path.join(unpacked, name))
    else:
        raise ValueError("scene_url must point to a .blend or a .zip holding one")
    blends = [os.path.join(r, n) for r, _, ns in os.walk(unpacked) for n in ns if n.lower().endswith(".blend")]
    if not blends:
        raise ValueError("the uploaded zip holds no .blend file")
    try:
        os.rename(unpacked, dest)
    except OSError:
        if not os.path.isdir(dest):
            raise
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def fetch_floor_plan(url, job_dir):
    """The order's floor plan as <job>/floor_plan.png (any image format in)."""
    from PIL import Image
    src = os.path.join(job_dir, "floor_plan_src")
    urllib.request.urlretrieve(url, src)
    Image.open(src).convert("RGB").save(os.path.join(job_dir, "floor_plan.png"))
    os.remove(src)


async def shoot(work, scene, job_id, request, uploader, effort="medium", resume=False, farm=None):
    """Run the shots agent on one order. Returns (director, response dict: stills, videos, result, missing, cost)."""
    d = ShotsDirector(work, scene, job_id, request, resume=resume, farm=farm, uploader=uploader, effort=effort)
    src = request.get("source") or {}
    d.emit("received", "running", f"{'resuming ' if resume else ''}{request.get('mode')} job {job_id} on "
           f"{src.get('build_id') or 'an uploaded scene'}: {len(request.get('shots', []))} shot(s)",
           source=src, mode=request.get("mode"), shots=request.get("shots"), settings=request.get("settings"),
           dials=request.get("dials"), floor_plan_url=request.get("floor_plan_url"))
    result = None
    try:
        result = await d.run(harness.agent_env())
    except Exception as e:  # deliver whatever exists rather than a bare 500
        import traceback
        print(traceback.format_exc(), flush=True)
        d.emit("error", "failed", f"shots stopped: {type(e).__name__}: {str(e)[:300]}")

    def files(sub, ext):
        p = os.path.join(d.out, sub)
        return [uploader(os.path.join(p, f)) for f in sorted(os.listdir(p)) if f.lower().endswith(ext)] \
            if os.path.isdir(p) else []
    rpath = os.path.join(d.out, "result.json")
    try:
        res_json = json.load(open(rpath, encoding="utf-8")) if os.path.exists(rpath) else {}
    except ValueError:
        res_json = {}
    return d, {"job_id": job_id, "stills": files("stills", ".png"), "videos": files("video", ".mp4"),
               "result": res_json, "missing": d.missing(), "agent_cost_usd": getattr(result, "total_cost_usd", None)}
