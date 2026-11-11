#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec /home/adminroot/miniconda3/envs/smart-help-web/bin/python deploy/stop.py
