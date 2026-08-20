#!/bin/sh
# ---------------------------------------------------------------------------
#  PithLocatorCT launcher.        Usage:  ./run.sh /path/to/cores
#
#  Same three stages as run.bat, in the same order: find a Python that actually
#  runs, offer to install one if there is none, and only then worry about
#  packages.
# ---------------------------------------------------------------------------
set -u
cd "$(dirname "$0")" || exit 1

echo
echo " PithLocatorCT - pith offset estimator"
echo " ------------------------------------"
echo

# ------------------------------------------------------------------- folder
FOLDER="${1-}"
if [ -z "$FOLDER" ]; then
    printf ' Folder with the cores: '
    read -r FOLDER
fi
# accept a path pasted with surrounding quotes, as a file manager may supply.
# Shell builtins only: this runs before we know anything about the machine.
FOLDER=${FOLDER#\"}
FOLDER=${FOLDER%\"}
if [ -z "$FOLDER" ]; then
    echo " No folder given."
    exit 1
fi
if [ ! -d "$FOLDER" ]; then
    echo " Not a folder: $FOLDER"
    exit 1
fi

# ---------------------------------------------------------- stage 1: python
# Validate by RUNNING each candidate, not by checking that it exists: a name on
# PATH can be a broken symlink or a shim that never executes Python.
PY=""
find_python() {
    PY=""
    for cand in "./python/bin/python3" "python3" "python" "py -3"; do
        if $cand -c "import sys" >/dev/null 2>&1; then
            PY="$cand"
            return 0
        fi
    done
    return 1
}

install_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "sudo apt install python3"
    elif command -v dnf >/dev/null 2>&1; then
        echo "sudo dnf install python3"
    elif command -v pacman >/dev/null 2>&1; then
        echo "sudo pacman -S python"
    elif command -v brew >/dev/null 2>&1; then
        echo "brew install python"
    else
        echo ""
    fi
}

if ! find_python; then
    echo " No working Python 3 was found on this machine."
    echo
    CMD=$(install_hint)
    if [ -n "$CMD" ]; then
        printf ' Install it with "%s"? [Y/n] ' "$CMD"
        read -r ANSWER
        case "${ANSWER:-Y}" in
            n|N|no|NO) CMD="" ;;
            *) sh -c "$CMD" ;;
        esac
        if [ -n "$CMD" ] && ! find_python; then
            echo
            echo " Python was installed but still cannot be found."
            exit 1
        fi
    fi
    if ! find_python; then
        echo
        echo " Python 3.8 or newer is required. Install it with your package"
        echo " manager, or from https://www.python.org/downloads/ , then run"
        echo " this script again."
        exit 1
    fi
fi
echo " Python:  $PY"

# ------------------------------------------------------- stage 3: packages
if ! $PY -c "import numpy, tifffile, PIL, openpyxl" >/dev/null 2>&1; then
    echo
    echo " Installing the Python packages this tool needs:"
    echo "    numpy, tifffile, pillow, openpyxl"
    echo
    if ! $PY -m pip install --user -r requirements.txt; then
        echo
        echo " That failed; retrying without --user."
        if ! $PY -m pip install -r requirements.txt; then
            echo
            echo " The packages could not be installed automatically. Run:"
            echo
            echo "    $PY -m pip install numpy tifffile pillow openpyxl"
            echo
            exit 1
        fi
    fi
fi

# -------------------------------------------------------------------- run
echo " Folder:  $FOLDER"
echo
exec $PY pithlocator.py "$FOLDER"
