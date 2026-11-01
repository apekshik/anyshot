"""The agent harness: one Claude agent (Claude Code through the Agent SDK) working in a workspace on a GPU worker.

It runs the agent with an instructions file appended to the Claude Code system prompt, gives it a `report` tool for
stage events, and streams everything it does as ANYSCENE_STAGE lines (stdout, which the fal queue relays to the caller
as logs) and into <out>/events.jsonl:
  - every tool call as a short `log` event (PostToolUse hook), with the image when it opens one;
  - a `heartbeat` every 20 s: what is running and for how long, plus the milestones so far;
  - its own text as `thinking` events.
A Stop hook keeps it working until `missing()` is empty. A cap guard stops it before fal's 2-hour request limit so
the endpoint can still package what exists, and a broken session is resumed (up to 3 times).

The same harness drives AnyScene's scene builder (github.com/apekshik/anyscene); the stage-line prefix is shared so
AnyScene can relay AnyShot's events into its own log unchanged.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
                              TextBlock, create_sdk_mcp_server, tool)

PREFIX = "ANYSCENE_STAGE "
# fal ends a request at 7200 s; warn the agent, then stop it with time left to package and upload
CAP_WARN_S = 95 * 60
CAP_STOP_S = 110 * 60
MODEL = "anthropic/claude-opus-5.5"  # through OpenRouter (OPENROUTER_API_KEY)
DIRECT_MODEL = "claude-opus-5-5"  # the same model on the Anthropic API (ANTHROPIC_API_KEY)


def user_env(name):
    """An environment variable; on Windows also the user's persistent environment (for local runs)."""
    if os.environ.get(name):
        return os.environ[name]
    if sys.platform == "win32":
        parts = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", name], capture_output=True, text=True).stdout.split()
        if name in parts:
            return parts[parts.index(name) + 2]
    return None


class Agent:
    PROMPT_PATH = ".anyshot/SHOTS.md"  # the instructions, relative to the workspace
    ASK = "Start by reading the instructions."

    def __init__(self, work, uploader=None, emit=None, effort="medium", out_rel="out"):
        self.effort = effort
        self.work = work
        self.out = os.path.join(work, out_rel)
        os.makedirs(self.out, exist_ok=True)
        self.t0 = time.time()
        self.upload = uploader or (lambda p: p)
        self.events = open(os.path.join(self.out, "events.jsonl"), "a", encoding="utf-8")
        self.log = open(os.path.join(self.out, "builder.log"), "a", encoding="utf-8")
        self.milestones = []  # every milestone so far, repeated in each heartbeat so a late watcher can backfill
        self.working = None  # (description, started) of the tool call in progress
        self.cap_warned = False
        self.capped = False  # stopped at the platform's 2-hour limit: let it end with what it has
        self._emit_hook = emit

    def elapsed(self):
        return time.time() - self.t0

    def emit(self, stage, status="running", message="", image=None, **extra):
        ev = {"t": round(self.elapsed(), 1), "stage": stage, "status": status, "message": message, **extra}
        if image:
            path = image if os.path.isabs(image) else os.path.join(self.work, image)
            if os.path.exists(path):
                ev["preview_url"] = self.upload(path)
        if stage not in ("log", "thinking", "heartbeat", "received", "error", "critique", "done", "packaged"):
            self.milestones.append({k: ev[k] for k in ("t", "stage", "status", "message", "preview_url") if k in ev})
        print(PREFIX + json.dumps(ev), flush=True)
        self.events.write(json.dumps(ev) + "\n")
        self.events.flush()
        if self._emit_hook:
            self._emit_hook(ev)

    def note(self, text):
        self.log.write(f"[{self.elapsed() / 60:6.1f}m] {text}\n")
        self.log.flush()

    # ---- MCP tools (a subclass adds its own)
    def tools(self):
        d = self

        @tool("report", "Post a stage event to the customer's live log, optionally with a rendered image.",
              {"stage": str, "message": str, "image": str})
        async def report(args):
            d.emit(args["stage"], "running", args.get("message", ""), args.get("image") or None)
            return {"content": [{"type": "text", "text": "reported"}]}

        return [report]

    # ---- hooks
    @staticmethod
    def describe(name, ti, work):
        if name == "Bash":
            return ti.get("description") or ti.get("command", "")[:120]
        if name in ("Write", "Edit"):
            return f"{'writing' if name == 'Write' else 'editing'} {os.path.relpath(ti.get('file_path', ''), work)}"
        if name == "Read":
            p = ti.get("file_path", "")
            return f"{'looking at' if p.lower().endswith(('.png', '.jpg', '.jpeg')) else 'reading'} {os.path.relpath(p, work)}"
        return name

    async def pre_tool(self, inp, tool_use_id, ctx):
        self.working = (self.describe(inp.get("tool_name", ""), inp.get("tool_input") or {}, self.work), time.time())
        if self.elapsed() > CAP_WARN_S and not self.cap_warned:
            self.cap_warned = True
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": (
                "The platform ends this request at 120 minutes and this order is now past 95. Before anything else, "
                "make sure every deliverable you have is saved where the instructions say, then keep going.")}}
        return {}

    async def heartbeat(self):
        """Every 20 s: what is running and for how long, plus the milestones so far. Long renders otherwise look idle,
        and a watcher that attaches late (fal's status call returns only the recent log) can fill in the schedule."""
        while True:
            await asyncio.sleep(20)
            w = self.working
            self.emit("heartbeat", "running", f"{self.elapsed() / 60:.0f} min in",
                      working=w[0] if w else None, working_s=round(time.time() - w[1]) if w else None,
                      milestones=self.milestones[-20:])

    async def post_tool(self, inp, tool_use_id, ctx):
        self.working = None
        name = inp.get("tool_name", "")
        ti = inp.get("tool_input") or {}
        quiet = name.startswith("mcp__scene__") or name in ("ToolSearch", "TodoWrite")
        msg = None if quiet else self.describe(name, ti, self.work)
        if msg:
            # an image the agent opens is shown to the customer too, so the live view follows what it is looking at;
            # the customer's own inputs are left out
            p = ti.get("file_path", "") if name == "Read" else ""
            image = p if p.lower().endswith((".png", ".jpg", ".jpeg")) and "/input/" not in p.replace("\\", "/") else None
            self.emit("log", "running", msg, image if image and os.path.exists(image) else None, tool=name)
        self.note(f"{name}: {json.dumps(ti)[:400]}")
        return {}

    def missing(self):
        """What is still missing from the deliverables; the agent is not let stop while this is non-empty."""
        return []

    async def on_stop(self, inp, tool_use_id, ctx):
        miss = self.missing()
        if miss and not self.capped:
            self.note(f"STOP blocked: {miss}")
            return {"decision": "block", "reason": "Not done: " + "; ".join(miss) + ". Keep going."}
        return {}

    def format_prompt(self, text):
        return text

    async def run(self, env):
        prompt = open(os.path.join(self.work, self.PROMPT_PATH), encoding="utf-8").read()
        prompt = self.format_prompt(prompt)
        server = create_sdk_mcp_server("scene", tools=self.tools())
        beat = asyncio.create_task(self.heartbeat())
        self.emit("received", "running", "agent starting")
        result, session_id, attempt = None, None, 0
        ask = self.ASK
        while True:
            opts = ClaudeAgentOptions(
                model=env.get("ANTHROPIC_MODEL") or MODEL, effort=self.effort, permission_mode="bypassPermissions",
                cwd=self.work,
                system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
                mcp_servers={"scene": server}, setting_sources=[], settings='{"fastMode": true}',
                hooks={"PreToolUse": [HookMatcher(hooks=[self.pre_tool])], "PostToolUse": [HookMatcher(hooks=[self.post_tool])],
                       "Stop": [HookMatcher(hooks=[self.on_stop])]},
                # tool results come back to us as single JSON lines; a full-size render read as an image is several
                # MB, and the default 1 MB buffer kills the session
                max_buffer_size=256 * 1024 * 1024, resume=session_id,
                env=env, stderr=lambda l: self.note("STDERR " + l[:300]))
            try:
                async with ClaudeSDKClient(opts) as client:
                    async def cap_guard():
                        # fal kills the request at 2 h with nothing returned; stop the agent first so the endpoint can
                        # still package and return what exists
                        while self.elapsed() < CAP_STOP_S:
                            await asyncio.sleep(15)
                        self.capped = True
                        self.emit("error", "running", "stopping the agent before the platform's 2-hour limit; "
                                  "packaging what it has")
                        await client.interrupt()
                    guard = asyncio.create_task(cap_guard())
                    try:
                        await client.query(ask)
                        async for m in client.receive_response():
                            session_id = getattr(m, "session_id", None) \
                                or (getattr(m, "data", None) or {}).get("session_id") or session_id
                            if isinstance(m, AssistantMessage):
                                for b in m.content:
                                    if isinstance(b, TextBlock) and b.text.strip():
                                        self.note("SAY " + b.text.strip()[:2000])
                                        self.emit("thinking", "running", b.text.strip()[:400])
                            elif isinstance(m, ResultMessage):
                                result = m
                    finally:
                        guard.cancel()
                break
            except Exception as e:  # the session broke (SDK or CLI error): pick the same session back up
                import traceback
                if self.capped:
                    break
                attempt += 1
                self.note("SESSION ERROR " + traceback.format_exc()[-3000:])
                self.emit("error", "running", f"agent session error ({type(e).__name__}: {str(e)[:200]}); "
                          f"{'resuming' if attempt <= 3 and session_id else 'giving up'}")
                if attempt > 3 or not session_id:
                    raise
                ask = ("The previous turn was cut off by a harness error, not by you. Continue from where you were; "
                       "check the state of the workspace first.")
        beat.cancel()
        cost = getattr(result, "total_cost_usd", None)
        self.emit("done", "done", f"agent finished after {self.elapsed() / 60:.0f} min"
                  + (f", agent cost ${cost:.2f}" if cost else ""), missing=self.missing())
        return result


def agent_env():
    """The agent's environment. Keys come only from the environment (fal secrets on a worker): OPENROUTER_API_KEY
    (Claude through OpenRouter, the default) or, without it, ANTHROPIC_API_KEY (the Anthropic API directly). FAL_KEY is
    passed through. ANTHROPIC_MODEL in the result is the model id for that provider."""
    key = user_env("OPENROUTER_API_KEY")
    direct = None if key else user_env("ANTHROPIC_API_KEY")
    if not key and not direct:
        raise RuntimeError("set OPENROUTER_API_KEY or ANTHROPIC_API_KEY (a fal secret on the app)")
    fal = user_env("FAL_KEY") or ""
    common = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "BASH_MAX_TIMEOUT_MS": "600000", "FAL_KEY": fal,
              "IS_SANDBOX": "1"}  # the fal worker runs as root in its own container; Claude Code needs this to skip prompts
    if direct:
        return {**common, "ANTHROPIC_BASE_URL": "https://api.anthropic.com", "ANTHROPIC_API_KEY": direct,
                "ANTHROPIC_MODEL": DIRECT_MODEL}
    return {**common, "ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": MODEL, "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "anthropic/claude-sonnet-5.5",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-5.5"}
