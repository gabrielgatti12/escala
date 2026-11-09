#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$repo_dir/deploy/.env" "${1:-86400}" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

path = Path(sys.argv[1])
interval = sys.argv[2]
if not interval.isdecimal() or int(interval) < 30:
    raise SystemExit('Informe um intervalo em segundos, de no mínimo 30.')
if not path.is_file():
    raise SystemExit('deploy/.env não encontrado. Configure o ambiente de produção primeiro.')
lines = path.read_text().splitlines()
pattern = re.compile(r'^\s*(?:export\s+)?SYNC_INTERVAL\s*=')
updated = [line for line in lines if not pattern.match(line)]
updated.append(f'SYNC_INTERVAL={int(interval)}')
mode = stat.S_IMODE(path.stat().st_mode)
temp_path = None
try:
    with tempfile.NamedTemporaryFile(mode='w', prefix='.env.tmp-', dir=path.parent, delete=False) as stream:
        temp_path = Path(stream.name)
        stream.write('\n'.join(updated) + '\n')
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)
finally:
    if temp_path:
        temp_path.unlink(missing_ok=True)
print(f'SYNC_INTERVAL configurado para {int(interval)} segundos. Recrie o serviço sync para aplicar.')
PY
