# Builds the symbolication test fixtures from the sources next to this file.
# Run ../generate.sh rather than this directly; it exports the build output
# into the fixture directories without bind-mounting anything.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates clang lld llvm g++ nodejs npm python3 \
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
