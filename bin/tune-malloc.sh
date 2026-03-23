#!/usr/bin/env sh
# Set MALLOC_ARENA_MAX to limit glibc memory arena proliferation.
#
# glibc defaults to 8 × nproc arenas. In containers, nproc reports the
# host's CPU count, not the cgroup limit — so a 2-CPU pod on an 8-core
# node gets 64 arenas. Each arena retains fragmented pages that are never
# returned to the OS, causing a memory staircase under sustained load.
#
# This reads the cgroup v2 CPU limit and sets MALLOC_ARENA_MAX to 2× the
# CPU count. Falls back to nproc if no cgroup limit is set.
#
# Skip if MALLOC_ARENA_MAX is already set explicitly.

if [ -z "$MALLOC_ARENA_MAX" ]; then
    CPUS=0
    if [ -f /sys/fs/cgroup/cpu.max ]; then
        # cgroup v2: cpu.max is "quota period" e.g. "350000 100000" = 3.5 CPUs
        # "max 100000" means unlimited
        read -r QUOTA PERIOD < /sys/fs/cgroup/cpu.max
        if [ "$QUOTA" != "max" ] && [ "$PERIOD" -gt 0 ] 2>/dev/null; then
            # Round up: (quota + period - 1) / period
            CPUS=$(( (QUOTA + PERIOD - 1) / PERIOD ))
        fi
    fi
    if [ "$CPUS" -lt 1 ]; then
        CPUS=$(nproc 2>/dev/null || echo 1)
    fi
    export MALLOC_ARENA_MAX=$(( CPUS * 2 ))
    unset CPUS QUOTA PERIOD
fi
