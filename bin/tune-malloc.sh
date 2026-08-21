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
#   to the OS immediately on free. Untuned this threshold is dynamic: it
#   starts at 128KB and is raised to the size of each large mmap'd block
#   that gets freed, up to 32MB, so a process that handles one big payload
#   stops using mmap for everything smaller than it did. Lowering it to
#   64KB keeps large transient allocations (JSON buffers, DB result sets,
#   pydantic model trees) out of the arenas they would otherwise fragment.
#
# MALLOC_TRIM_THRESHOLD_ — How much free space at the top of the heap glibc
#   keeps before returning it to the OS. Untuned it is dynamic too, tracking
#   twice the mmap threshold (so it also climbs, to as much as 64MB).
#   Lowering it to 64KB makes freed pages go back sooner.
#
# Setting either threshold explicitly is also what stops glibc adjusting them
# at runtime, which is half the point: it makes the behavior the same on the
# first request and the millionth.
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
    elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
        # cgroup v1: quota and period in separate files, quota -1 = unlimited.
        # Without this branch a v1 host falls through to nproc, which reports
        # host CPUs -- the very thing this block exists to avoid.
        QUOTA=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
        PERIOD=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null || echo 0)
        if [ "$QUOTA" -gt 0 ] && [ "$PERIOD" -gt 0 ] 2>/dev/null; then
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
