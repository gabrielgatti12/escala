#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$EUID" != 0 ]]; then
    echo 'Execute este instalador com sudo na instância Oracle.' >&2
    exit 1
fi
if [[ $# != 1 ]]; then
    echo 'Uso: sudo bash scripts/configure-google-editor.sh /caminho/cliente-oauth.json' >&2
    exit 1
fi
python3 - "$1" <<'PY'
import json,sys
with open(sys.argv[1]) as stream:
    config=json.load(stream).get('web',{})
if not config.get('client_id') or not config.get('client_secret') or not config.get('redirect_uris'):
    raise SystemExit('Arquivo inválido: baixe um cliente OAuth do tipo Aplicativo da Web com URI de retorno.')
PY
chmod 600 -- "$1"
install -o root -g 10001 -m 640 -- "$1" deploy/google-sheets-oauth-client.json
python3 - <<'PY'
import os,base64
from pathlib import Path
path=Path('deploy/google-sheets-token-key.txt')
if not path.exists():
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(base64.urlsafe_b64encode(os.urandom(32))+b'\n')
os.chown(path,0,10001)
os.chmod(path,0o640)
PY
echo 'Arquivos privados instalados. Recrie o serviço web com scripts/oracle-compose.sh e conecte a conta Google pela tela de teste.'
