# Benchmarks

## Memory Growth Benchmark (`bench_ingest_memory.py`)

Measures web server memory growth under sustained load with artificial database
latency. Uses `tc netem` to inject round-trip delay on the web container's
connections to Postgres and Valkey, then fires high-concurrency traffic in waves
while sampling cgroup memory.

**Why:** In production, GlitchTip's web process grows from ~300 MB to ~3 GB over
several days. This benchmark compresses that pattern into minutes by combining
forced backend latency with high concurrency, making it possible to measure the
effect of optimizations locally.

### Quick start

```bash
# Default: ingest-only, 500 concurrent, 5 waves of 2000 events, 100ms DB latency
bash benchmarks/run_ingest_bench.sh

# Mixed workload (ingest + API reads + uptime checks), 15 waves
bash benchmarks/run_ingest_bench.sh --mode mixed -c 500 -n 2000 -w 15

# Quick smoke test
bash benchmarks/run_ingest_bench.sh -c 50 -n 200 -w 2 --pause 1
```

### What it does

1. Starts a dedicated `compose.bench.yml` stack (web + postgres + valkey + bench)
2. Waits for web to finish migrations and bootstrap
3. Installs `iproute2` in the web container and applies `tc netem` delay
4. Runs `bench_ingest_memory.py` from the bench container
5. Samples `memory.current` from the web container's cgroup every 0.5s
6. Prints a memory timeline and summary showing baseline, peak, and growth

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `DB_LATENCY_MS` | `100` | Artificial round-trip latency to Postgres/Valkey |
| `DB_JITTER_MS` | `10` | Latency jitter |

All arguments after `run_ingest_bench.sh` are forwarded to the Python script:

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `ingest` | `ingest` (envelope only) or `mixed` (realistic workload) |
| `-c` | `500` | Concurrent requests |
| `-n` | `2000` | Requests per wave |
| `-w` | `5` | Number of waves |
| `--pause` | `3.0` | Seconds between waves |

### Mixed workload weights

The `--mode mixed` option distributes requests across endpoint types:

| Type | Weight | Endpoint |
|------|--------|----------|
| `ingest` | 60 | `POST /api/{id}/envelope/` |
| `uptime` | 20 | `GET /api/0/organizations/{slug}/heartbeat_check/{uuid}/` |
| `list_issues` | 8 | `GET /api/0/organizations/{slug}/issues/` |
| `list_projects` | 4 | `GET /api/0/projects/` |
| `get_org` | 4 | `GET /api/0/organizations/{slug}/` |
| `get_project` | 4 | `GET /api/0/projects/{org}/{proj}/` |

### Notes

- The compose stack runs with `DEBUG=false` so Django's query logging and debug
  middleware don't pollute memory measurements.
- The web container has `cap_add: NET_ADMIN` to allow `tc` traffic shaping.
- First run on a fresh DB takes longer (~2-3 min) for migrations and partition
  creation. Subsequent runs reuse the existing DB and start in seconds.
- The bench container stays running (`sleep infinity`) between runs so you can
  re-run the Python script manually without restarting the stack.
- The memory limit is set to 1 GB. Increase it in `compose.bench.yml` if you
  want to run longer tests.

## Cold Storage Benchmark (`bench_cold_storage.py`)

Measures archive and cleanup performance at various data volumes.

```bash
docker compose run --rm web python manage.py shell \
    -c "exec(open('benchmarks/bench_cold_storage.py').read())"
```
