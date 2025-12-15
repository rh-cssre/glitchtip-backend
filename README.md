[![Gitter](https://badges.gitter.im/GlitchTip/community.svg)](https://gitter.im/GlitchTip/community?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge)

<script src="https://liberapay.com/GlitchTip/widgets/button.js"></script>

<noscript>
    <a href="https://liberapay.com/GlitchTip/donate">
        <img alt="Donate using Liberapay" src="https://liberapay.com/assets/widgets/donate.svg">
    </a>
</noscript>

# GlitchTip Backend

GlitchTip is an open source, Sentry API compatible error tracking platform. It is a partial fork/mostly re-implementation
of Sentry's open source codebase before it went proprietary. Its goals are to be a modern, easy-to-develop error
tracking platform that respects your freedom to use it any way you wish. Some differences include:

- A modern development environment with Python 3, Django 5, async, and types.
- Simplicity over features. We use Postgres to store error data. Our code base is a fraction of the size of Sentry and
  looks like a typical Django app. We leverage existing open source Django ecosystem apps whenever possible.
- Lightweight - GlitchTip runs with as little as 1GB of ram, PostgreSQL, and Redis.
- Respects your privacy. No massive JS bundles. No invasive tracking. No third party spying. Our marketing site runs the
  privacy-focused Plausible analytics. Self hosted GlitchTip will never report home. We will never know if you run it
  yourself.
- Commitment to open source. We use open source tools like GitLab whenever possible. With our MIT license, you can use
  it for anything you'd like and even sell it. We believe in competition and hope you make GlitchTip even better.

GlitchTip is a stable platform used in production environments for several years.

# Developing

We use Docker for development.
View our [Contributing](./CONTRIBUTING.md) documentation if you'd like to help make GlitchTip better.
See [API Documentation](https://app.glitchtip.com/api/docs)

## Run local dev environment

1. Ensure docker and docker-compose are installed
2. Execute `docker compose up` (or `make start`)
3. Execute `docker compose run --rm web ./manage.py migrate` (or `make migrate`)

Run tests with `docker compose run --rm web ./manage.py test` (or `make test`) and see the logs with
`docker compose logs -ft` (or `make logs`).

Execute `make help` for more shortcuts.

### Run HTTPS locally for testing FIDO2 keys

1. `cp compose.yml compose.override.yml`
2. Edit the override file and set `command: ./manage.py runsslserver 0.0.0.0:8000`
3. Restart docker compose services

### Run with advanced partitioning

Using [`pg_partman`](https://github.com/pgpartman/pg_partman) requires the extension to be installed in postgres.
`compose.part.yml` offers an alternative image. Switching between advanced and default partitioning is not supported.

Execute the containers by running:

```shell
docker compose -f compose.yml -f compose.part.yml up
# or: make partman-start
```

This automatically configures `pg_partman` but you can update it manually with `./manage.py setup_advanced_partitions`.

Default partitioning uses `DATE` partitions managed by Django. Advanced partitioning uses nested `ORG_ID HASH > DATE`
partitions managed by `pg_partman`.

### VS Code (Optional)

VS Code can do type checking and type inference. However, it requires setting up a virtual environment.

1. Install Python. For Ubuntu this is `apt install python3-dev python3-venv`
2. Install [poetry](https://python-poetry.org/docs/#installation)
3. Create Python virtual environment `python -m venv env`
4. Activate environment `source env/bin/activate`
5. Install packages `poetry install`

### Load testing

We use [Locust](https://locust.io/) to load test. It's built into the dev dependencies.

First, set the env var `IS_LOAD_TEST` to true in `compose.yml`, then run:

```shell
docker compose -f compose.yml -f compose.locust.yml up
# or: make locust-start
```

Now go to [localhost:8089](http://localhost:8089/) to run the test.

> Note: Locust will not be installed to production docker images and cannot be run from them.


### Memory profiling

Use memray to profile. For example we can profile celery beat (bin/run-beat.sh) with

```shell
exec memray run /usr/local/bin/celery -A glitchtip beat -s /tmp/celerybeat-schedule -l info --pidfile=
```

Then run `memray flamegraph file.bin` on the above file output.

### Observability metrics with Prometheus

1. Edit `monitoring/prometheus/prometheus.yml` and set credentials to a GlitchTip auth token
2. Execute `docker compose -f compose.yml -f compose.metrics.yml up`

# GCP Logging

In order to enable json logging, set the environment as follows::

```
DJANGO_LOGGING_HANDLER_CLASS=google.cloud.logging_v2.handlers.ContainerEngineHandler
UWSGI_LOG_ENCODER='json {"severity":"info","timestamp":${unix},"message":"${msg}"}}'
```

# Acknowledgements

- Thank you to the Sentry team for their ongoing open source SDK work and formerly open source backend of which this
  project is based on.
- We use element.io for our public gitter room
- Plausible Analytics is used for analytics
- Django - no other web framework is as feature complete
- django-ninja/Pydantic - brings typed and async-first api design
