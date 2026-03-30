#!/usr/bin/env sh
# Tune glibc malloc for long-running Python processes in containers.
#
# Problem: glibc's malloc retains freed memory in per-thread arenas and
# large sub-allocations, causing RSS to grow as a staircase over hours.
# Three tunables address this:
#
# MALLOC_ARENA_MAX — Limits the number of arenas. glibc defaults to
#   8 × nproc, but in containers nproc reports the host CPU count, not
#   the cgroup limit. We set it to 2× the container's CPU quota.
#
# MALLOC_MMAP_THRESHOLD_ — Allocations above this size use mmap/munmap
#   directly instead of arena sub-allocation. mmap'd pages are returned
#   to the OS immediately on free. Default is dynamic (up to 512KB);
#   lowering to 64KB prevents large transient allocations (JSON buffers,
#   DB result sets, pydantic model trees) from fragmenting arenas.
#
# MALLOC_TRIM_THRESHOLD_ — Controls how aggressively glibc trims the
#   top of the heap. Default is 128KB; lowering to 64KB makes it return
#   freed pages sooner.
#
# Benchmarked effect: MMAP+TRIM thresholds at 64KB reduced baseline RSS
# by ~260MB (483MB → 223MB) with no throughput impact.
#
# All three skip if already set explicitly, so operators can override.

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

if [ -z "$MALLOC_MMAP_THRESHOLD_" ]; then
    export MALLOC_MMAP_THRESHOLD_=65536
fi

if [ -z "$MALLOC_TRIM_THRESHOLD_" ]; then
    export MALLOC_TRIM_THRESHOLD_=65536
fi
