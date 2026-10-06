# Builds the symbolication test fixtures from the sources next to this file.
# Run ../generate.sh rather than this directly; it exports the build output
# into the fixture directories without bind-mounting anything.
#
# Everything is pinned so a rebuild reproduces the committed files byte for
# byte: the base image by digest, apt packages by a snapshot.debian.org
# timestamp, esbuild by version. ../TOOLCHAIN.txt is the expected tool
# versions; a rebuild that prints different ones will produce different
# fixtures.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS build

ARG SNAPSHOT=20260920T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} trixie main" \
        > /etc/apt/sources.list \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${SNAPSHOT} trixie-security main" \
        >> /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates clang lld llvm g++ binutils nodejs npm python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tools
RUN npm install --no-audit --no-fund esbuild@0.25.10

# Fixed source paths, so the paths embedded in the debug info and sourcemaps
# do not depend on where the generator was run from.
COPY native /src/native
COPY js /src/js
COPY build.sh symbols.py /tools/
RUN sh /tools/build.sh /out

FROM scratch
COPY --from=build /out /
