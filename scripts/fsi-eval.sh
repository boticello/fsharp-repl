#!/bin/sh
# fsi-eval — one-shot evaluation for a configured F# project.
#
#   scripts/fsi-eval.sh '1 + 2'
#
# An argument is one expression and its value is printed. `-` reads a full
# F# script from stdin; `--file PATH` executes a full script in place (so
# relative #load/#r paths keep their normal FSI meaning). Each invocation
# starts a fresh `dotnet fsi`. An optional fsrepl.json supplies the preload.

set -eu

if [ "$#" -eq 1 ]; then
    mode=expression
    [ "$1" = - ] && mode=stdin
elif [ "$#" -eq 2 ] && [ "$1" = "--file" ]; then
    mode=file
    [ -f "$2" ] || { echo "error: script file not found: $2" >&2; exit 1; }
    script=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
else
    echo "usage: $0 '<f# expression>' | - | --file PATH" >&2
    exit 1
fi

tool_root=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "${FSREPL_PROJECT_ROOT:-.}" && pwd)
python3 "$tool_root/scripts/fsi-config.py" prepare "$root"
preload=$(python3 "$tool_root/scripts/fsi-config.py" preload "$root")

cd "$root"
if [ "$mode" = file ]; then
    if [ -n "$preload" ]; then
        exec dotnet fsi "--use:$preload" --exec --quiet "$script"
    fi
    exec dotnet fsi --exec --quiet "$script"
fi

# FSI script mode needs a real file. Copy stdin byte-for-byte into one; never
# place source on the command line of a second shell.
dir=$(mktemp -d)
tmp="$dir/input.fsx"
trap 'rm -rf "$dir"' EXIT INT TERM HUP

if [ "$mode" = stdin ]; then
    cat > "$tmp"
else
    printf 'printfn "%%A" (%s)\n' "$1" > "$tmp"
fi

if [ -n "$preload" ]; then
    dotnet fsi "--use:$preload" --exec --quiet "$tmp"
else
    dotnet fsi --exec --quiet "$tmp"
fi
