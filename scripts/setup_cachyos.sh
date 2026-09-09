#!/usr/bin/env bash
set -euo pipefail
if ! command -v python >/dev/null 2>&1; then sudo pacman -S --needed python; fi
if ! python -c 'import tkinter' >/dev/null 2>&1; then sudo pacman -S --needed tk; fi
printf 'Block-n-Pick dependencies ready.
'
