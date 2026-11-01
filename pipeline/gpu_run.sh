#!/usr/bin/env bash
# Run a command holding the shared GPU, first come first served, so parallel scene builds never render in Cycles at once
# (16 GB card) and nobody can re-grab the GPU ahead of a caller that is already waiting.
#   pipeline/gpu_run.sh "$B" -b ...
# Each caller drops a ticket named <epoch-ns>_<pid> in .gpu.queue and runs once its ticket sorts first.
# Tickets whose process is gone, or older than 90 minutes, are cleared.
Q="$(dirname "$0")/../.gpu.queue"
mkdir -p "$Q"
TICKET="$Q/$(date +%s%N)_$$"
echo "$(date -Is) $*" > "$TICKET"
trap 'rm -f "$TICKET"' EXIT
while :; do
  for t in "$Q"/*; do
    [ "$t" = "$TICKET" ] && continue
    pid="${t##*_}"
    if ! kill -0 "$pid" 2>/dev/null || [ -n "$(find "$t" -maxdepth 0 -mmin +90 2>/dev/null)" ]; then
      echo "gpu_run: clearing stale ticket $(basename "$t")" >&2
      rm -f "$t"
    fi
  done
  [ "$(ls "$Q" | sort | head -n1)" = "$(basename "$TICKET")" ] && break
  sleep 5
done
"$@"
