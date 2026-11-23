#!/bin/sh
# Double-click launcher for macOS: runs launch.sh in a Terminal window and keeps the window open on errors.
cd "$(dirname "$0")" || exit 1
sh ./launch.sh "$@"
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
    echo
    echo "Lineup stopped with exit code $status. Press Return to close this window."
    read -r _
fi
exit "$status"
