#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f deploy/compose.yaml)
if [[ -f deploy/google-service-account.json ]]; then
    compose+=(-f deploy/compose.drive.yaml)
fi
if [[ -f deploy/google-drive-api-key.txt ]]; then
    compose+=(-f deploy/compose.drive-key.yaml)
fi
if [[ -f deploy/google-sheets-oauth-client.json && -f deploy/google-sheets-token-key.txt ]]; then
    compose+=(-f deploy/compose.sheets-editor.yaml)
fi
exec "${compose[@]}" "$@"
