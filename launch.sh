#!/bin/sh
# Lineup launcher for Linux and macOS: finds Python 3.10 or newer, prepares a private .venv on the first run,
# starts lineup.py. Arguments are passed on (for example: ./launch.sh --game "$HOME/Games/GTA VI" --build).
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Lineup needs Python 3.10 or newer, and none was found." >&2
    echo "Install it with your package manager (for example: sudo apt install python3 python3-venv)," >&2
    echo "from https://www.python.org/downloads/ or with Homebrew (brew install python), then run this again." >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Creating a private Python environment in .venv (first run only) ..."
    if ! "$PY" -m venv .venv; then
        rm -rf .venv
        echo "Could not create .venv. On Debian and Ubuntu the venv module is a separate package:" >&2
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
fi

if ! cmp -s requirements.txt .venv/requirements.installed; then
    echo "Installing numpy and Pillow into .venv (needs an internet connection the first time) ..."
    if ! .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
        echo "Installing the packages failed. Check your internet connection and run this again." >&2
        echo "If it keeps failing, delete the .venv folder and try once more." >&2
        exit 1
    fi
    cp requirements.txt .venv/requirements.installed
fi

exec .venv/bin/python lineup.py "$@"
