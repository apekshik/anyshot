# Sourced by every shell script: finds the repo root, Blender and ffmpeg.
#   . pipeline/env.sh            (from the repo root)
# Override any of them in your shell: BLENDER=/path/to/blender FFMPEG=/path/to/ffmpeg
ANYSCENE_ROOT_POSIX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Blender and ffmpeg on Windows want C:/... paths, not Git Bash's /c/...
ROOT_ABS="$(cd "$ANYSCENE_ROOT_POSIX" && (pwd -W 2>/dev/null || pwd))"
export ANYSCENE_ROOT="$ROOT_ABS"
# Machine-specific overrides (BLENDER=..., FFMPEG=...) go in pipeline/env.local.sh, which is gitignored.
[ -f "$ANYSCENE_ROOT_POSIX/pipeline/env.local.sh" ] && . "$ANYSCENE_ROOT_POSIX/pipeline/env.local.sh"

if [ -z "$BLENDER" ]; then
  for c in blender \
           "/c/Program Files/Blender Foundation/Blender 5.0/blender.exe" \
           "/Applications/Blender.app/Contents/MacOS/Blender"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then BLENDER="$c"; break; fi
  done
fi
export BLENDER FFMPEG="${FFMPEG:-ffmpeg}"
# Short names the scene scripts use.
B="$BLENDER"
FF="$FFMPEG"
[ -n "$B" ] || echo "env.sh: Blender not found; set BLENDER=/path/to/blender" >&2
