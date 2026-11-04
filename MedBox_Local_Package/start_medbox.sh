#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/server"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
exec .venv/bin/python app.py
