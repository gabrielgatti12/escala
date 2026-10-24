#!/usr/bin/env bash
set -euo pipefail
cd /workspace/escala
python_bin=/workspace/.escala-venv/bin/python
read -r -a sheet_ids <<< "${SHEET_IDS:-}"
pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT
trap 'exit 0' INT TERM
"$python_bin" -u manage.py sync_sheets "${sheet_ids[@]}" --registered --interval "${SYNC_INTERVAL:-300}" &
pids+=("$!")
"$python_bin" -u manage.py runserver 127.0.0.1:8000 --noreload &
pids+=("$!")
wait -n "${pids[@]}"
