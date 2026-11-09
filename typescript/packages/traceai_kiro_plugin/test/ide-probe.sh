#!/usr/bin/env bash
# Cloud-only distribution probe. This is NOT GUI/host-hook/model or trace evidence.
set -euo pipefail
mkdir -p /work/evidence
DOWNLOAD=$(mktemp -d /tmp/kiro-download.XXXXXX)
EXTRACT=$(mktemp -d /tmp/kiro-extract.XXXXXX)
HOME_DIR=$(mktemp -d /tmp/kiro-probe-home.XXXXXX)
URL=https://prod.download.desktop.kiro.dev/releases/stable/linux-x64/signed/1.4.121/tar/kiro-ide-1.4.121-stable-linux-x64.tar.gz
curl -fSL --max-time 180 "$URL" -o "$DOWNLOAD/kiro.tar.gz"
sha256sum "$DOWNLOAD/kiro.tar.gz" > /work/evidence/ide-distribution.sha256
python3 -I -c 'import sys,tarfile; t=tarfile.open(sys.argv[1]); t.extractall(sys.argv[2], filter="data")' "$DOWNLOAD/kiro.tar.gz" "$EXTRACT"
ROOT=$(find "$EXTRACT" -type f -path '*/resources/app/package.json' -print -quit | sed 's@/resources/app/package.json$@@')
if [ -z "$ROOT" ]; then echo 'IDE_LAYOUT_UNKNOWN'; exit 1; fi
# No app launch or sign-in. Electron runs only the vendor CLI --version entry point.
apt-get update -qq
apt-get install -y -qq libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libgtk-3-0
if apt-cache show libasound2t64 >/dev/null 2>&1; then apt-get install -y -qq libasound2t64; else apt-get install -y -qq libasound2; fi
ldd "$ROOT/kiro" > /work/evidence/ide-libraries.txt
HOME="$HOME_DIR" ELECTRON_RUN_AS_NODE=1 timeout 20 "$ROOT/kiro" "$ROOT/resources/app/out/cli.js" --version > /work/evidence/ide-version.txt 2> /work/evidence/ide-version.stderr
python3 -I - "$ROOT/resources/app" <<'PY' > /work/evidence/ide-contract-source.txt
import pathlib,sys,re,json
root=pathlib.Path(sys.argv[1])
pkg=json.loads((root/'package.json').read_text())
print('IDE package version:',pkg.get('version'))
# Vendor code is data: never import, evaluate or execute extension source.
patterns=['USER_PROMPT','hook_event_name','session_id','tool_response','tool_use_id','PreTaskExec','PostTaskExec']
for p in root.glob('extensions/kiro*/**/*.js'):
    if not p.is_file() or 'node_modules' in p.parts or p.stat().st_size>60*1024*1024: continue
    text=p.read_text(errors='replace')
    hits=[key for key in patterns if key in text]
    if not hits: continue
    print('\nSOURCE',p.relative_to(root), 'FIELDS',','.join(hits))
    anchor=text.find('function v8t(')
    if anchor >= 0: print('IDE_HOOK_PAYLOAD_SERIALIZER', text[anchor:anchor+6000])
    for key in hits:
        for m in list(re.finditer(re.escape(key),text))[:3]:
            print(key, text[max(0,m.start()-160):m.end()+260])
PY
printf '%s\n' 'Distribution/version inspection only: GUI activation, hook consent and real IDE spans remain UNVERIFIED.' > /work/evidence/ide-scope.txt
