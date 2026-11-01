"""AnyShot as a fal Serverless app: an AI cinematographer for 3D scenes (see README.md).

    fal deploy app.py::AnyShotApp --app-name anyshot --auth private
    python client.py video --scene examples/tea_room.zip --shot "slow push along the counter, ending on the tin"

POST /scene2image   a scene + shots in words or by name  ->  finished, graded stills (rendered on this worker's GPU)
POST /scene2video   a scene + shots in words or by name  ->  camera moves as mp4s (frames rendered on the farm app)

The scene is either `scene_url` (a .blend, or a .zip holding a .blend and its textures) or `build_id` (an AnyScene
build on the same fal account's storage). A Claude agent (agent/director.py, instructions in agent/SHOTS.md) reads
the set, plans every camera (moves, lenses, focus pulls, a path drawn on an optional floor plan), checks clearance on
every frame, blocks with quick previews, then renders finals. It streams ANYSCENE_STAGE lines that the fal queue relays
to the caller as logs.

Configuration is all environment (fal secrets): OPENROUTER_API_KEY or ANTHROPIC_API_KEY, FAL_KEY,
FAL_SERVERLESS_KEY (optional: the key the farm is called with; defaults to FAL_KEY) and ANYSHOT_FARM_APP (the farm app
id; a request's `farm_app` overrides it for that order).
"""

import json
import os
import sys
from pathlib import PurePosixPath

import fal
from fal.container import ContainerImage
from pydantic import BaseModel, Field

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "agent"))
import harness  # noqa: E402  (the agent harness: hooks, stage events, heartbeat, cap guard)
import director  # noqa: E402  (ShotsDirector, the farm_render tool, scene fetching)

if os.name == "nt":
    # Deploying from Windows: ContainerImage's context_dir default is a WindowsPath Linux can't unpickle.
    _posix = PurePosixPath("/tmp")
    ContainerImage.__dataclass_fields__["context_dir"].default = _posix
    ContainerImage.context_dir = _posix
    ContainerImage.__init__.__defaults__ = tuple(
        _posix if isinstance(d, os.PathLike) else d for d in ContainerImage.__init__.__defaults__ or ()
    )

TOOLKIT = director.toolkit_files()  # SHOTS.md, camera/, pipeline/: read at deploy time, carried in the app
BLENDER_GPU = open(os.path.join(_HERE, "agent", "blender_gpu.py"), encoding="utf-8").read()
OPTIX_CACHE_LOCAL = "/var/tmp/OptixCache_root/optix7cache.db"
OPTIX_CACHE_SHARED = "/data/anyshot/cache/optix7cache.db"
BUILDS_DIR = "/data/anyscene/builder"  # AnyScene builds (build_id) on this account's storage
SHOT_DIR = "/data/anyshot"  # uploaded scenes (scenes/<scene_id>) and the retry index (_index)
BLENDER_VERSION = "5.0.1"
BLENDER_WRAPPER = f'#!/bin/sh\nexec /usr/local/bin/blender.real --python {director.BLENDER_GPU_PATH} "$@"\n'
WARMUP = """import bpy
s = bpy.context.scene
s.render.engine = "CYCLES"; s.cycles.samples = 4; s.render.resolution_x = s.render.resolution_y = 64
m = bpy.data.materials.new("glass"); m.use_nodes = True; m.node_tree.nodes.new("ShaderNodeBsdfGlass")
bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0.5))
bpy.context.object.data.materials.append(m)
bpy.ops.render.render()
print("WARMUP done")
"""

# the same image as AnyScene's scene builder, so the layers are shared when both run on one account
DOCKERFILE = f"""
FROM python:3.13-slim
ENV NVIDIA_DRIVER_CAPABILITIES=all
RUN apt-get update && apt-get install -y --no-install-recommends \\
      curl ca-certificates xz-utils git procps fonts-dejavu-core libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 \\
      libegl1 libxkbcommon0 libsm6 libice6 libx11-6 libglib2.0-0 ffmpeg nodejs ripgrep zip \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://download.blender.org/release/Blender{BLENDER_VERSION[:3]}/blender-{BLENDER_VERSION}-linux-x64.tar.xz \\
      | tar -xJ -C /opt \\
    && ln -s /opt/blender-{BLENDER_VERSION}-linux-x64/blender /usr/local/bin/blender.real
"""


def save_optix_cache():
    """Copy this worker's OptiX cache to /data if it has grown, so new workers skip the ~2 min kernel compile."""
    import shutil
    if os.path.exists(OPTIX_CACHE_LOCAL) and (not os.path.exists(OPTIX_CACHE_SHARED)
                                              or os.path.getsize(OPTIX_CACHE_LOCAL) > os.path.getsize(OPTIX_CACHE_SHARED)):
        os.makedirs(os.path.dirname(OPTIX_CACHE_SHARED), exist_ok=True)
        tmp = f"{OPTIX_CACHE_SHARED}.{os.getpid()}.tmp"
        shutil.copy(OPTIX_CACHE_LOCAL, tmp)
        os.replace(tmp, OPTIX_CACHE_SHARED)


class Shot(BaseModel):
    name: str = Field("", description="A camera or move by name: one of an AnyScene build's scouting cameras "
                                      "(its shots.json), a camera in the uploaded .blend, or a new name for the output.")
    description: str = Field("", description="The shot in words, e.g. 'slow push along the pastry case, ending close "
                                             "on the croissants, rack focus to the price card'. With a name, it adjusts "
                                             "that camera.")


class ShotRequest(BaseModel):
    scene_url: str | None = Field(None, description="The scene: a .blend, or a .zip holding a .blend and its textures "
                                                    "(any public or signed URL). Give this or build_id.")
    build_id: str | None = Field(None, description="Or an AnyScene build (b_...) on the same fal account's storage.")
    prompt: str = Field("", description=(
        "Direction for the whole order in your own words, like you'd brief a camera crew: 'a 45 second trailer: "
        "open wide, walk in like a visitor, then three slow dolly reveals with rack focus; moody, let the caustics "
        "move'. With no shots, the agent plans the shot list from this; with shots, it applies to all of them."))
    shots: list[Shot] = Field(default_factory=list, description="The shots to make, by name or in words. Optional "
                                                                "when prompt describes them.")
    floor_plan_url: str | None = Field(None, description="Optional plan of the set (image), optionally with a camera "
                                                         "path or camera marks drawn on it; the agent follows it.")
    settings: dict = Field(default_factory=dict, description=(
        "Optional: res [w, h], samples, fps, seconds (per video shot), lens_mm, handheld (none|walk|subtle), "
        "look (clean|cinematic|gritty|vintage). Missing ones are chosen per shot."))
    dials: dict[str, float | str] = Field(default_factory=dict, description=(
        "Optional changes to the set for this order only (time_of_day, atmosphere, power, warmth, wetness, ...); "
        "the source scene is never changed."))
    effort: str = Field("medium", description="Claude effort for the agent: low, medium, high, xhigh, max.")
    fresh: bool = Field(False, description="Shoot again even if an identical request already has a result.")
    farm_app: str | None = Field(None, description=(
        "Advanced: the render farm app (<owner>/<app>, farm/app.py deployed on the same account) for this order's "
        "video frames. Default: the ANYSHOT_FARM_APP secret."))


class ShotResponse(BaseModel):
    job_id: str
    scene_id: str = Field(description="The build_id, or the id of the uploaded scene's workspace (s_...).")
    stills: list[str]
    videos: list[str]
    result: dict = Field(description="Per shot: the camera, lens, move and settings used, and a note.")
    missing: list[str]
    agent_cost_usd: float | None
    minutes: float


def _stage(stage, status, message):
    print(harness.PREFIX + json.dumps({"t": 0, "stage": stage, "status": status, "message": message}), flush=True)


class AnyShotApp(fal.App):
    machine_type = ["GPU-RTX5090", "GPU-RTXPRO6000"]
    image = ContainerImage.from_dockerfile_str(DOCKERFILE)
    requirements = ["pydantic", "anthropic", "pillow", "fal==1.84.0", "fal-client", "claude-agent-sdk==0.2.176"]
    local_python_modules = ["harness", "director"]
    min_concurrency = 0
    max_concurrency = 10
    request_timeout = 7200  # fal's cap; the agent stops at 110 min to deliver, and a retried request resumes
    startup_timeout = 1200

    def setup(self):
        import shutil
        import subprocess
        import time
        os.makedirs(os.path.dirname(director.BLENDER_GPU_PATH), exist_ok=True)
        with open(director.BLENDER_GPU_PATH, "w") as f:
            f.write(BLENDER_GPU)
        with open("/usr/local/bin/blender", "w") as f:
            f.write(BLENDER_WRAPPER)
        os.chmod("/usr/local/bin/blender", 0o755)
        if os.path.exists(OPTIX_CACHE_SHARED):
            os.makedirs(os.path.dirname(OPTIX_CACHE_LOCAL), exist_ok=True)
            shutil.copy(OPTIX_CACHE_SHARED, OPTIX_CACHE_LOCAL)
        t = time.time()
        r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", WARMUP], capture_output=True, text=True,
                           timeout=900)
        print(f"setup: OptiX warm-up {'ok' if 'WARMUP done' in r.stdout else 'FAILED'} in {time.time() - t:.0f} s", flush=True)
        save_optix_cache()

    @fal.endpoint("/scene2image")
    async def scene2image(self, req: ShotRequest) -> ShotResponse:
        """Finished, graded stills of a 3D scene: cameras named or described in words."""
        return await self._order(req, "images")

    @fal.endpoint("/scene2video")
    async def scene2video(self, req: ShotRequest) -> ShotResponse:
        """Camera moves through a 3D scene, planned and clearance-checked by the agent, rendered on the farm."""
        return await self._order(req, "video")

    async def _order(self, req: ShotRequest, mode: str) -> ShotResponse:
        import asyncio
        import hashlib
        import time
        import uuid

        t0 = time.time()
        if bool(req.scene_url) == bool(req.build_id):
            raise ValueError("give exactly one of scene_url or build_id")
        if not req.shots and not req.prompt.strip():
            raise ValueError("give shots, a prompt, or both")
        if req.build_id and not req.build_id.replace("_", "").isalnum():
            raise ValueError("bad build_id")
        director.farm_app(req.farm_app)  # validates the override early

        # Retry-safe: fal's queue re-runs a request that times out or loses its worker. Each identical order maps to
        # one job; a repeat returns the finished result, attaches to a live attempt, or resumes a dead one in the same
        # job folder (frames already on disk are not rendered again). The farm doesn't change the result, so it is
        # left out of the fingerprint.
        order = {"mode": mode, "scene_url": req.scene_url, "build_id": req.build_id, "floor_plan": req.floor_plan_url,
                 "prompt": req.prompt, "shots": [s.model_dump() for s in req.shots], "settings": req.settings, "dials": req.dials,
                 "effort": req.effort}
        fp = hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()[:20]
        index = os.path.join(SHOT_DIR, "_index", fp + ".json")
        os.makedirs(os.path.dirname(index), exist_ok=True)
        resume, rec = False, None
        if not req.fresh and os.path.exists(index):
            told = False
            while True:
                rec = json.load(open(index))
                if rec.get("response"):
                    _stage("done", "done", f"identical request: returning finished job {rec['job_id']}")
                    return ShotResponse(**rec["response"])
                alive = os.path.join(rec["work"], "out", "shots", rec["job_id"], ".alive")
                if not os.path.exists(alive) or time.time() - os.path.getmtime(alive) > 90:
                    break  # its attempt died: resume it below
                if not told:
                    _stage("received", "running", f"identical request: attached to job {rec['job_id']}, still running")
                    told = True
                await asyncio.sleep(20)
            resume = True
        else:
            if req.build_id:
                work, scene_id = os.path.join(BUILDS_DIR, req.build_id), req.build_id
            else:
                scene_id = "s_" + hashlib.sha256(req.scene_url.encode()).hexdigest()[:12]
                work = os.path.join(SHOT_DIR, "scenes", scene_id)
            rec = {"job_id": "j_" + uuid.uuid4().hex[:8], "work": work, "scene_id": scene_id, "fingerprint": fp,
                   "started": time.time()}
            json.dump(rec, open(index, "w"))
        job_dir = os.path.join(rec["work"], "out", "shots", rec["job_id"])
        os.makedirs(job_dir, exist_ok=True)

        async def keep_alive():  # proves this attempt is alive to any retry of the same request
            while True:
                open(os.path.join(job_dir, ".alive"), "w").close()
                await asyncio.sleep(20)
        alive_task = asyncio.create_task(keep_alive())
        try:
            return await self._order_in(req, mode, rec, index, job_dir, resume, t0)
        finally:
            alive_task.cancel()

    async def _order_in(self, req, mode, rec, index, job_dir, resume, t0) -> ShotResponse:
        import asyncio
        import time

        from fal.toolkit import File

        upload = lambda p: File.from_path(p).url
        work, job_id = rec["work"], rec["job_id"]
        if req.build_id:
            if not os.path.isdir(work):
                raise ValueError(f"build {req.build_id} is not on this account's storage")
            scene = director.build_scene_name(work)
            source = {"kind": "build", "build_id": req.build_id}
        else:
            scene = "scene"
            _stage("received", "running", "fetching the scene")
            await asyncio.to_thread(director.fetch_scene, work, req.scene_url)
            for d in (f"scenes/{scene}", "out"):
                os.makedirs(os.path.join(work, d), exist_ok=True)
            source = {"kind": "upload", "scene_url": req.scene_url, "files": "input/scene/"}
        director.write_toolkit(work, TOOLKIT)  # the current tools and SHOTS.md; never the scene's own files
        request = {"mode": mode, "source": source, "prompt": req.prompt, "shots": [s.model_dump() for s in req.shots],
                   "settings": req.settings, "dials": req.dials, "floor_plan_url": req.floor_plan_url, "floor_plan": None}
        if req.floor_plan_url:
            if not os.path.exists(os.path.join(job_dir, "floor_plan.png")):
                await asyncio.to_thread(director.fetch_floor_plan, req.floor_plan_url, job_dir)
            request["floor_plan"] = os.path.relpath(os.path.join(job_dir, "floor_plan.png"), work).replace(os.sep, "/")

        d, out = await director.shoot(work, scene, job_id, request, upload, effort=req.effort, resume=resume,
                                      farm=req.farm_app)
        res = ShotResponse(**out, scene_id=rec["scene_id"], minutes=round((time.time() - t0) / 60, 1))
        save_optix_cache()
        d.emit("packaged", "done", f"{len(res.stills)} stills, {len(res.videos)} videos after {res.minutes:.0f} min",
               stills=res.stills, videos=res.videos)
        if not res.missing:  # a repeat of this order now gets this result; an incomplete one is resumed instead
            rec = json.load(open(index))
            rec.update(response=res.model_dump(), finished=time.time())
            json.dump(rec, open(index, "w"))
        await asyncio.sleep(2)  # let the last stage lines reach the queue logs before COMPLETED
        return res
