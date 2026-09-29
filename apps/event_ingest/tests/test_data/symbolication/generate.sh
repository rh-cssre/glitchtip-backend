#!/bin/sh
# Rebuild the symbolication fixtures in native/ and js/ from the sources in
# generator/. Needs only docker with BuildKit: the build runs in a pinned
# Debian image, nothing is bind-mounted, and the output is exported straight
# into this directory. The build is byte-for-byte reproducible.
#
# What is here, and where it comes from:
#   native/crash.elf           g++ -g, x86_64 ELF with its own DWARF
#   native/crash.stripped.elf  the same ELF after objcopy --strip-debug
#   native/crash.macho.dwarf   clang + ld64.lld + dsymutil, arm64 dSYM companion
#   native/crash.fat.macho.dwarf  the universal dSYM companion of that arm64
#                              image lipo'd with an x86_64 build: one file,
#                              two slices, two debug ids
#   native/crash.pdb           clang-cl + lld-link /DEBUG /Brepro, x86_64 PDB
#   native/symbols.json        per image: function starts, and the addresses a
#                              real crash reports (return addresses from
#                              llvm-objdump, the faulting store located with
#                              llvm-symbolizer)
#   js/*.min.js, *.min.js.map  esbuild bundles of generator/js, with a debug id
#                              stamped in the way SDK build tooling does
#   TOOLCHAIN.txt              the tool versions; a rebuild printing different
#                              ones will not reproduce the files
#   proguard/                  a hand-written R8 mapping and the Kotlin sources
#                              it describes (not generated)
#   flutter/                   not generated here: the gzipped Dart AOT
#                              debug symbols (--split-debug-info) of a small
#                              Flutter test app built for x86_64 Android, and
#                              the absolute return addresses of one of its
#                              crashes (no debug_meta, loaded at
#                              0x75fe1ec1a000). A dense symbol table, where
#                              image base estimation must refuse.
# The native images are built from generator/native/crash.cpp, which the
# tests also read for source context: edit it only together with a rebuild.
# Every source was written for these tests. The release-bundle tests also use
# ../bundle.js, an existing fixture. The multi-megabyte bundle is generated at
# test time (make_large_bundle in ../../symbolication.py), not committed.
#
# After regenerating, refresh the goldens and review the diff:
#   docker compose run --rm -e UPDATE_SYMBOLICATION_GOLDENS=1 web \
#       python manage.py test apps.event_ingest.tests.test_symbolication_parity
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build --output "type=local,dest=$HERE" "$HERE/generator"
# The local exporter marks the directories it writes setgid and sticky.
find "$HERE" -type d -exec chmod g-s,-t {} +
find "$HERE/native" "$HERE/js" -type f -exec chmod a-x {} +
