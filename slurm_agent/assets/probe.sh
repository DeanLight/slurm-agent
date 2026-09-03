#!/bin/sh
# The one-round-trip poll. Piped to `sh -s <run_root>` on the login node.
#
# Emits ONE json object: the queue, and every run root with its status block, its
# notebook's observed mtime and size, and the allocation's GPU utilisation. That is
# everything the supervision loop needs, in one ssh round trip, however many agents are in
# flight — which is what makes polling every five minutes affordable over a 2FA'd link.
#
# No dependency is installed on the cluster: this script is the whole mechanism.
set -u
RUN_ROOT="${1:-$HOME/.slurm-agent/runs}"
FMT='Name:|,JobID:|,NodeList:|,StateCompact:|,TimeLeft:|,TimeUsed:|,tres-alloc:|'

printf '{"now": %s, "queue": "' "$(date +%s)"
squeue --me --noheader --Format="$FMT" 2>/dev/null | sed 's/"/\\"/g; s/$/\\n/' | tr -d '\n'
printf '", "runs": ['

first=1
for dir in "$RUN_ROOT"/*/; do
    [ -f "$dir/launch.json" ] || continue
    [ $first -eq 1 ] || printf ','
    first=0
    nb=$(sed -n 's/.*"notebook": *"\([^"]*\)".*/\1/p' "$dir/launch.json" | head -1)
    ipynb="${nb%.py}.ipynb"
    mtime=0; bytes=0
    if [ -f "$ipynb" ]; then
        mtime=$(stat -c %Y "$ipynb" 2>/dev/null || stat -f %m "$ipynb" 2>/dev/null || echo 0)
        bytes=$(stat -c %s "$ipynb" 2>/dev/null || stat -f %z "$ipynb" 2>/dev/null || echo 0)
    fi
    smtime=0
    [ -f "$dir/status.json" ] && smtime=$(stat -c %Y "$dir/status.json" 2>/dev/null \
        || stat -f %m "$dir/status.json" 2>/dev/null || echo 0)

    printf '{"run_dir": "%s", "nb_mtime": %s, "nb_bytes": %s, "status_mtime": %s, "launch": ' \
        "${dir%/}" "$mtime" "$bytes" "$smtime"
    cat "$dir/launch.json" | tr -d '\n'
    printf ', "status": '
    if [ -f "$dir/status.json" ]; then cat "$dir/status.json" | tr -d '\n'; else printf 'null'; fi
    printf '}'
done
printf ']}'
