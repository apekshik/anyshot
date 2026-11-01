"""Render farm: headless Blender on fal Serverless GPUs (AnyShot's /scene2video renders its frames here).

One request = one chunk of frames. The scene arrives as a self-contained bundle zip (scene.blend plus whatever the
Blender arguments reference) behind a download URL; each runner downloads a bundle once to its own local disk, so later
chunks of the same scene on that runner start rendering straight away. Frames go back as a zip on fal's file store,
so the worker never holds storage credentials.

    fal deploy farm/app.py::BlenderFarm --app-name blender-farm --auth private

Request: bundle_url, bundle_id (the bundle's sha256 prefix), blender_args (after `blender -b`; {bundle} and {out}
expand to the unpacked bundle and the output folder), optional timeout_s and outputs (globs of the files to return,
default PNGs only). Any client can use it, not just AnyShot: AnyScene's Blender add-on renders the open scene on it.
"""

import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import fal
from fal.container import ContainerImage

from pydantic import BaseModel, Field

if os.name == "nt":
    # Deploying from Windows: ContainerImage's context_dir default is the cwd as a WindowsPath, evaluated at import.
    # It gets pickled along with the app and Linux can't unpickle a WindowsPath, so swap it for a POSIX path.
    _posix = PurePosixPath("/tmp")
    ContainerImage.__dataclass_fields__["context_dir"].default = _posix
    ContainerImage.context_dir = _posix  # dataclasses also keep the default as a class attribute
    ContainerImage.__init__.__defaults__ = tuple(
        _posix if isinstance(d, os.PathLike) else d for d in ContainerImage.__init__.__defaults__ or ()
    )

BLENDER_VERSION = "5.0.1"

DOCKERFILE = f"""
FROM python:3.13-slim
# Expose the driver's graphics libraries too: OptiX (libnvoptix) ships with them, not with plain compute.
ENV NVIDIA_DRIVER_CAPABILITIES=all
RUN apt-get update && apt-get install -y --no-install-recommends \\
      curl ca-certificates xz-utils libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libegl1 \\
      libxkbcommon0 libsm6 libice6 libx11-6 libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://download.blender.org/release/Blender{BLENDER_VERSION[:3]}/blender-{BLENDER_VERSION}-linux-x64.tar.xz \\
      | tar -xJ -C /opt \\
    && ln -s /opt/blender-{BLENDER_VERSION}-linux-x64/blender /usr/local/bin/blender
"""

# A plain string: a Path built here would be pickled as WindowsPath when deploying from Windows.
# Local disk, not /data: a bundle on the shared /data mount is read by every worker at once, and on a 42-worker fan-out
# of a 1.6 GB set that contention held each worker ~30 min before its first frame (alone, the same set renders its first
# frame in 17 s). Each worker downloads the bundle from the CDN instead, which serves parallel downloads well.
CACHE_DIR = "/tmp/anyscene-farm/bundles"

# OptiX compiles its kernels on a worker's first Cycles render (~2 min) into a local sqlite cache. Without help every
# new worker pays that before its first frame, which on a 100-worker fan-out was most of the bill. Keep the cache on
# /data per GPU model, start each worker from it, warm it in setup() and save it back when it grows.
OPTIX_CACHE_LOCAL_DIR = "/var/tmp/OptixCache_root"
OPTIX_CACHE_SHARED_DIR = "/data/anyscene-farm/optix"
WARMUP = """import bpy
prefs = bpy.context.preferences.addons["cycles"].preferences
prefs.compute_device_type = "OPTIX"
prefs.get_devices()
for d in prefs.devices:
    d.use = d.type == "OPTIX"
s = bpy.context.scene
s.render.engine = "CYCLES"; s.cycles.device = "GPU"; s.cycles.samples = 4
s.render.resolution_x = s.render.resolution_y = 64
glass = bpy.data.materials.new("glass"); glass.use_nodes = True
nt = glass.node_tree; nt.links.new(nt.nodes.new("ShaderNodeBsdfGlass").outputs[0], nt.nodes["Material Output"].inputs[0])
bpy.ops.mesh.primitive_cube_add(size=4)
haze = bpy.data.materials.new("haze"); haze.use_nodes = True
nt = haze.node_tree; nt.links.new(nt.nodes.new("ShaderNodeVolumePrincipled").outputs[0], nt.nodes["Material Output"].inputs["Volume"])
bpy.context.object.data.materials.append(haze)
bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0.5))
bpy.context.object.data.materials.append(glass)
bpy.ops.render.render()
print("WARMUP done")
"""


def optix_shared_dir(device: str) -> str:
    """The shared cache for this GPU model and driver: kernels compiled on one don't load on another."""
    slug = "".join(c if c.isalnum() else "_" for c in device)[:80]
    return f"{OPTIX_CACHE_SHARED_DIR}/{slug}"


def load_optix_cache(device: str):
    src = optix_shared_dir(device)
    if os.path.isdir(src):
        shutil.copytree(src, OPTIX_CACHE_LOCAL_DIR, dirs_exist_ok=True)


def save_optix_cache(device: str):
    """Copy this worker's cache to /data if it has grown (atomic per file; concurrent workers just race)."""
    if not os.path.isdir(OPTIX_CACHE_LOCAL_DIR):
        return
    dst = optix_shared_dir(device)
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(OPTIX_CACHE_LOCAL_DIR):
        a, b = os.path.join(OPTIX_CACHE_LOCAL_DIR, name), os.path.join(dst, name)
        if os.path.isfile(a) and (not os.path.exists(b) or os.path.getsize(a) > os.path.getsize(b)):
            tmp = f"{b}.{os.getpid()}.tmp"
            shutil.copy(a, tmp)
            os.replace(tmp, b)


class RenderJob(BaseModel):
    bundle_url: str = Field(description="Download URL of the scene bundle zip (e.g. a presigned R2 link).")
    bundle_id: str = Field(description="Content hash of the bundle; runners cache bundles by this id.")
    blender_args: list[str] = Field(
        description="Arguments after `blender -b`. {bundle} expands to the unpacked bundle dir, {out} to the output dir."
    )
    timeout_s: int = Field(3000, description="Kill Blender after this many seconds.")
    outputs: list[str] = Field(
        ["**/*.png"],
        description="Glob patterns (relative to {out}) of the files to return. The default is the original PNG-only "
                    "behaviour; ['frames/**/*'] returns EXR, JPEG, TIFF... exactly as Blender wrote them.",
    )


class RenderResult(BaseModel):
    frames_url: str = Field(description="Zip of the rendered frames on fal storage.")
    frame_count: int
    render_seconds: float
    bundle_seconds: float
    device: str
    log_tail: str


def fetch_bundle(url: str, bundle_id: str) -> Path:
    """Download and unpack a bundle once per cache; concurrent requests on the same runner wait on a lock."""
    import fcntl  # Linux-only; the worker runs Linux, but `fal deploy` imports this module locally too

    CACHE = Path(CACHE_DIR)

    if not bundle_id.isalnum():
        raise ValueError("bundle_id must be alphanumeric")
    dest = CACHE / bundle_id
    if (dest / ".ready").exists():
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / f"{bundle_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (dest / ".ready").exists():
            return dest
        tmp = CACHE / f"{bundle_id}.part"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        zpath = tmp / "bundle.zip"
        sha = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=600) as r, open(zpath, "wb") as f:
            while chunk := r.read(8 << 20):
                sha.update(chunk)
                f.write(chunk)
        if not sha.hexdigest().startswith(bundle_id):
            raise ValueError(f"bundle hash mismatch: got {sha.hexdigest()[:16]}")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp / "b")
        zpath.unlink()
        shutil.rmtree(dest, ignore_errors=True)
        (tmp / "b").rename(dest)
        shutil.rmtree(tmp, ignore_errors=True)
        (dest / ".ready").touch()
    return dest


class BlenderFarm(fal.App):
    # RTX 5090 (32 GB) by default: $2.50/h vs $4.00/h for the RTX PRO 6000 (96 GB) at about 10% fewer cores. Switch to
    # "GPU-RTXPRO6000" for sets that need more than 32 GB of VRAM.
    machine_type = "GPU-RTX5090"
    # No COPY steps, so no build context is needed; a pure POSIX path keeps the pickled app loadable on Linux when
    # it is deployed from Windows (the default context_dir is a WindowsPath).
    image = ContainerImage.from_dockerfile_str(DOCKERFILE, context_dir=PurePosixPath("/tmp"))
    requirements = ["pydantic", "fal==1.84.0"]  # fal.toolkit for uploading the frames zip
    min_concurrency = 0
    keep_alive = 300  # a burst of orders reuses warm workers (booted, kernels compiled) instead of booting new ones
    max_concurrency = 100  # account cap raised to 100 (Max Concurrency Cap); FARM_WORKERS on the shots app matches
    request_timeout = 3600
    startup_timeout = 1800

    def setup(self):
        smi = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True)
        self.device = smi.stdout.strip() or "unknown GPU"
        ver = subprocess.run(["blender", "-b", "--version"], capture_output=True, text=True)
        load_optix_cache(self.device)
        t = time.time()
        r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", WARMUP], capture_output=True,
                           text=True, timeout=900)
        warm = f"OptiX warm-up {'ok' if 'WARMUP done' in r.stdout else 'FAILED'} in {time.time() - t:.0f} s"
        save_optix_cache(self.device)
        print("farm ready:", self.device, "|", ver.stdout.splitlines()[0] if ver.stdout else ver.stderr[-200:], "|", warm)

    @fal.endpoint("/")
    def render(self, job: RenderJob) -> RenderResult:
        # Imported here, not at module level: fal.toolkit holds Path constants that pickle as WindowsPath when the
        # app is deployed from Windows, and the Linux worker then can't load the app.
        from fal.toolkit import File

        t0 = time.time()
        bundle = fetch_bundle(job.bundle_url, job.bundle_id)
        t1 = time.time()
        out = Path(f"/tmp/out-{os.getpid()}-{int(t1 * 1000)}")
        out.mkdir(parents=True)
        args = [a.replace("{bundle}", str(bundle)).replace("{out}", str(out)) for a in job.blender_args]
        proc = subprocess.run(["blender", "-b", *args], capture_output=True, text=True, timeout=job.timeout_s, cwd=bundle)
        t2 = time.time()
        save_optix_cache(self.device)  # a scene can need kernels the warm-up didn't compile
        log = (proc.stdout + proc.stderr)[-4000:]
        pngs = sorted({p for pat in job.outputs for p in out.glob(pat) if p.is_file()})
        if proc.returncode != 0 or not pngs:
            raise RuntimeError(f"blender exited {proc.returncode} with {len(pngs)} frames\n{log}")
        zpath = out.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # image formats are already compressed
            for p in pngs:
                z.write(p, p.relative_to(out).as_posix())
        result = RenderResult(
            frames_url=File.from_path(zpath, content_type="application/zip").url,
            frame_count=len(pngs),
            render_seconds=round(t2 - t1, 1),
            bundle_seconds=round(t1 - t0, 1),
            device=self.device,
            log_tail=log[-1500:],
        )
        shutil.rmtree(out, ignore_errors=True)
        zpath.unlink(missing_ok=True)
        return result
