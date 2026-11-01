"""AnyShot command-line client: send a scene and shot directions, follow the agent's live log, save the results.

    pip install fal-client            # or: uv run --with fal-client python client.py ...
    export FAL_KEY=...                # a key of the account the app is deployed under
    export ANYSHOT_APP=<your-fal-username>/anyshot

    python client.py image --scene examples/tea_room.zip --shot "wide establishing shot from the doorway, 24mm"
    python client.py video --scene ./set.zip --shot "slow push along the counter, ending on the register"         --settings '{"seconds": 6, "res": [1920, 1080]}' --floor-plan ./plan_with_path.png
    python client.py video --build-id b_1234567890 --shot hero_wide --shot "orbit the bar, 35mm"

--scene and --floor-plan take a local file (uploaded to fal storage first) or a URL. Every stage event is written to
<out>/events.jsonl; the response goes to <out>/response.json and the stills/videos next to it.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

STAGE = "ANYSCENE_STAGE "  # the agent's stage events in the queue logs (agent/harness.py PREFIX)


def main():
    p = argparse.ArgumentParser(description="AnyShot: shots of a 3D scene, planned by an AI cinematographer.")
    p.add_argument("mode", choices=["image", "video"], help="image: /scene2image (stills). video: /scene2video.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--scene", help="a .blend, or a .zip holding a .blend and its textures (path or URL)")
    src.add_argument("--build-id", help="an AnyScene build (b_...) on the app's account")
    p.add_argument("--prompt", default="", help="direction for the whole order in words; enough on its own")
    p.add_argument("--shot", action="append", default=[],
                   help="a camera name, or a shot in words (repeatable); 'name: words' gives both")
    p.add_argument("--floor-plan", help="optional plan image, optionally with a camera path drawn on it (path or URL)")
    p.add_argument("--settings", default="{}", help='JSON, e.g. {"seconds": 6, "fps": 24, "res": [1920, 1080]}')
    p.add_argument("--dials", default="{}", help='JSON, e.g. {"time_of_day": "night", "atmosphere": 0.6}')
    p.add_argument("--effort", default="medium")
    p.add_argument("--fresh", action="store_true", help="shoot again even if an identical order already finished")
    p.add_argument("--farm-app", help="advanced: the render farm app (<owner>/<app>) for this order's video frames "
                                      "(default: the app's ANYSHOT_FARM_APP secret)")
    p.add_argument("--app", default=os.environ.get("ANYSHOT_APP"), help="fal app id (default: $ANYSHOT_APP)")
    p.add_argument("--out", default="anyshot_out")
    a = p.parse_args()
    if not a.shot and not a.prompt:
        p.error("give --prompt, --shot, or both")
    if not a.app:
        p.error("set ANYSHOT_APP=<owner>/<app> or pass --app")
    if not os.environ.get("FAL_KEY"):
        p.error("set FAL_KEY to a key of the account the app is deployed under")
    sys.stdout.reconfigure(encoding="utf-8")
    import fal_client

    up = lambda s: s if s is None or s.startswith(("http://", "https://")) else fal_client.upload_file(s)
    shots = []
    for s in a.shot:
        name, sep, words = s.partition(":")
        if sep and name.strip().replace("_", "").isalnum():
            shots.append({"name": name.strip(), "description": words.strip()})
        elif " " in s.strip():
            shots.append({"description": s.strip()})
        else:
            shots.append({"name": s.strip()})
    payload = {"prompt": a.prompt, "shots": shots, "settings": json.loads(a.settings), "dials": json.loads(a.dials), "effort": a.effort,
               "fresh": a.fresh, "floor_plan_url": up(a.floor_plan)}
    if a.farm_app:
        payload["farm_app"] = a.farm_app
    if a.scene:
        payload["scene_url"] = up(a.scene)
    else:
        payload["build_id"] = a.build_id

    os.makedirs(a.out, exist_ok=True)
    events = open(os.path.join(a.out, "events.jsonl"), "a", encoding="utf-8")
    t0, seen = time.time(), set()

    def on_update(update):
        for log in getattr(update, "logs", None) or []:
            msg = log.get("message", "")
            if msg in seen or STAGE not in msg:
                continue
            seen.add(msg)
            ev = json.loads(msg.split(STAGE, 1)[1])
            if ev.get("stage") == "heartbeat":
                continue
            events.write(json.dumps(ev) + "\n")
            events.flush()
            line = f"[{(time.time() - t0) / 60:5.1f}m] {ev['stage']:<10} {ev.get('message', '')[:160]}"
            if ev.get("preview_url"):
                line += f"\n{'':19}image: {ev['preview_url']}"
            print(line, flush=True)

    route = "scene2image" if a.mode == "image" else "scene2video"
    res = fal_client.subscribe(f"{a.app}/{route}", arguments=payload, with_logs=True, on_queue_update=on_update,
                               on_enqueue=lambda rid: print(f"request {rid}", flush=True), client_timeout=3 * 3600)
    with open(os.path.join(a.out, "response.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    for url in res.get("stills", []) + res.get("videos", []):
        urllib.request.urlretrieve(url, os.path.join(a.out, url.rsplit("/", 1)[-1]))
    print(json.dumps({k: v for k, v in res.items() if k != "result"}, indent=1))


if __name__ == "__main__":
    main()
