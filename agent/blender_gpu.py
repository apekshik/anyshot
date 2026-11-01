"""Every Blender the agent starts renders Cycles on the GPU, even with --factory-startup and no GPU prefs script.
On the fal worker `blender` is a wrapper that runs this first (`blender.real --python blender_gpu.py "$@"`): headless
Blender otherwise renders on the CPU, which an agent forgot once and lost most of a two-hour job to."""

import bpy
from bpy.app.handlers import persistent


def _devices():
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except (KeyError, AttributeError):
        return
    for kind in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
        except TypeError:
            continue
        if any(d.type == kind for d in prefs.devices):
            for d in prefs.devices:
                d.use = d.type == kind
            return


@persistent
def _gpu_scenes(*_):
    for sc in bpy.data.scenes:
        sc.cycles.device = "GPU"


_devices()
_gpu_scenes()
bpy.app.handlers.load_post.append(_gpu_scenes)
bpy.app.handlers.render_init.append(_gpu_scenes)
