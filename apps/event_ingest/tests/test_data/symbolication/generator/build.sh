#!/bin/sh
# Runs inside the generator image. Writes every fixture under $1.
set -eu
OUT="$1"
# Debian installs the unversioned LLVM tool names (ld64.lld, dsymutil, ...)
# only under the versioned prefix.
PATH="$(echo /usr/lib/llvm-*/bin):$PATH"
export PATH
mkdir -p "$OUT/native" "$OUT/js"

# --- Native: one C++ source, three object formats -------------------------
cd /src/native

# ELF (x86_64), linked normally; the binary carries its own DWARF.
g++ -g -O0 -fno-exceptions -fno-rtti -fno-omit-frame-pointer \
    -Wl,--build-id=sha1 -o crash.elf crash.cpp

# Mach-O (arm64). A linked Mach-O keeps its DWARF in the .o files, so
# dsymutil gathers it into a dSYM; the dSYM's DWARF companion is what gets
# uploaded. No libc is linked: the program calls nothing outside itself.
clang++ --target=arm64-apple-macos11 -g -O0 -fno-exceptions -fno-rtti \
    -c crash.cpp -o crash.macho.o
ld64.lld -arch arm64 -platform_version macos 11.0 11.0 -e _main \
    -o crash.macho crash.macho.o
dsymutil crash.macho -o crash.macho.dSYM

# PE (x86_64) with a PDB. /Brepro makes the PDB GUID a content hash, so a
# rebuild from the same source reproduces the same debug id.
clang --driver-mode=cl --target=x86_64-pc-windows-msvc /Z7 /Od /GS- /GR- /EHs-c- \
    /c crash.cpp /Focrash.obj
lld-link /DEBUG:FULL /PDB:crash.pdb /NODEFAULTLIB /ENTRY:main \
    /SUBSYSTEM:CONSOLE /OUT:crash.exe /Brepro /MAP:crash.map crash.obj

cp crash.elf "$OUT/native/crash.elf"
cp crash.macho.dSYM/Contents/Resources/DWARF/crash.macho "$OUT/native/crash.macho.dwarf"
cp crash.pdb "$OUT/native/crash.pdb"
cp crash.cpp "$OUT/native/crash.cpp"
python3 /tools/symbols.py crash.elf crash.macho crash.map > "$OUT/native/symbols.json"

# --- JavaScript: esbuild bundles with sourcemaps --------------------------
ESBUILD=/tools/node_modules/.bin/esbuild

cd /src/js/debugid-app
mkdir -p node_modules
cp -r /src/js/deps/tiny-format node_modules/tiny-format
"$ESBUILD" src/index.js --bundle --minify --sourcemap --sources-content=true \
    --format=iife --outfile=dist/app.min.js

cd /src/js/second-app
"$ESBUILD" src/widget.js --bundle --minify --sourcemap --sources-content=true \
    --format=iife --outfile=dist/widget.min.js

# Stamp a debug id the way the SDK tooling does: a debugId key in the map and
# a trailing debugId comment in the minified file. The id is derived from the
# bundle's own content, so it is stable across rebuilds.
python3 - "$OUT/js" <<'PY'
import hashlib
import json
import sys
import uuid

out = sys.argv[1]
for src, name in [
    ("/src/js/debugid-app/dist/app.min.js", "app.min.js"),
    ("/src/js/second-app/dist/widget.min.js", "widget.min.js"),
]:
    with open(src, "rb") as f:
        code = f.read()
    with open(src + ".map") as f:
        sourcemap = json.load(f)
    debug_id = str(uuid.UUID(hashlib.sha1(code).hexdigest()[:32]))
    sourcemap["debugId"] = debug_id
    code = code.rstrip(b"\n") + f"\n//# debugId={debug_id}\n".encode()
    with open(f"{out}/{name}", "wb") as f:
        f.write(code)
    with open(f"{out}/{name}.map", "w") as f:
        json.dump(sourcemap, f, separators=(",", ":"))
PY

# Toolchain versions, for anyone comparing a rebuild.
{
    g++ --version | head -n1
    clang --version | head -n1
    ld.lld --version | head -n1
    "$ESBUILD" --version | sed 's/^/esbuild /'
} > "$OUT/TOOLCHAIN.txt"
