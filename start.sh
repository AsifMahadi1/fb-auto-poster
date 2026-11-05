#!/usr/bin/env bash
# Start FB Auto Poster on 0.0.0.0:8069 using Python 3.8.
set -euo pipefail

cd "$(dirname "$0")"

exec python3.8 run.py
