#!/bin/sh
# Rebuild the symbolication fixtures in native/ and js/ from the sources in
# generator/. Needs only docker with BuildKit: the build runs in a pinned
# Debian image, nothing is bind-mounted, and the output is exported straight
# into this directory. The build is byte-for-byte reproducible.
#
# What is here, and where it comes from:
#   native/crash.elf          g++ -g, x86_64 ELF with its own DWARF
#   native/crash.macho.dwarf  clang + ld64.lld + dsymutil, arm64 dSYM companion
#   native/crash.pdb          clang-cl + lld-link /DEBUG /Brepro, x86_64 PDB
#   native/crash.cpp          the source all three are built from
#   native/symbols.json       function offsets in each image (from llvm-nm / the
#                             lld-link map), so tests can build frame addresses
#   js/*.min.js, *.min.js.map esbuild bundles of generator/js, with a debug id
#                             stamped in the way SDK build tooling does
#   proguard/                 a hand-written R8 mapping and the Kotlin sources
#                             it describes (not generated)
# Every source was written for these tests. The release-bundle tests also use
# ../bundle.js, an existing fixture. The multi-megabyte bundle is generated at
# test time (make_large_bundle in ../../symbolication.py), not committed.
#
# After regenerating, refresh the goldens and review the diff:
#   UPDATE_SYMBOLICATION_GOLDENS=1 docker compose run --rm web \
#       python manage.py test apps.event_ingest.tests.test_symbolication_parity
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build --output "type=local,dest=$HERE" "$HERE/generator"
# The local exporter marks the directories it writes setgid and sticky.
find "$HERE" -type d -exec chmod g-s,-t {} +
