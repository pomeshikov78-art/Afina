#!/bin/bash
# Сборка офлайн-бандла Linux (Astra Linux SE / Ubuntu / ALT): deb-пакет + tar.xz
set -e
VER=${1:-0.1.0}
OUT=dist/afina-$VER
mkdir -p $OUT
cp -r ../src/afina ../src/client ../data $OUT/
cp installer/run_server.pyw $OUT/ 2>/dev/null || cp run_server.pyw $OUT/
python3 - <<EOF
import hashlib, json, os
files={}
for root,_,fns in os.walk("$OUT"):
    for fn in fns:
        p=os.path.join(root,fn); rel=os.path.relpath(p,"$OUT")
        files[rel]=hashlib.sha256(open(p,"rb").read()).hexdigest()
json.dump({"version":"$VER","files":files},open("$OUT/bundle_manifest.json","w"),indent=1)
print("манифест:",len(files),"файлов")
EOF
tar cJf dist/afina-$VER-linux.tar.xz -C dist afina-$VER
echo "OK: dist/afina-$VER-linux.tar.xz (deb: см. README — dpkg-deb по шаблону)"
