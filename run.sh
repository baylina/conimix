#!/bin/bash
# Arrenca SpotiTxell. Requereix yt-dlp i ffmpeg al PATH i SPOTITXELL_PASSWORD (mínim 8 caràcters).
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/python server.py
