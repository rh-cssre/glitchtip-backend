FROM python:3.14 AS build-python
ARG IS_CI
ENV PYTHONUNBUFFERED=1 \
  UV_COMPILE_BYTECODE=1 \
  UV_SYSTEM_PYTHON=true \
  UV_PYTHON_DOWNLOADS=never \
  UV_PROJECT_ENVIRONMENT=/usr/local \
  PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /code
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock /code/
RUN uv sync --frozen --no-install-project $(test "$IS_CI" = "True" && echo "--no-dev")

FROM python:3.14-slim
ARG GLITCHTIP_VERSION=local
ENV GLITCHTIP_VERSION ${GLITCHTIP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb/extensions

RUN apt-get update && apt-get install -y libxml2 libpq5 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY --from=build-python /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=build-python /usr/local/bin/ /usr/local/bin/

EXPOSE 8000

COPY . /code/
ARG COLLECT_STATIC
RUN if [ "$COLLECT_STATIC" != "" ] ; then SECRET_KEY=ci ./manage.py collectstatic --noinput; fi

# Pre-install DuckDB extensions at build time so nothing is downloaded at runtime.
# Extension version is locked to the duckdb version in uv.lock.
# Stored in a shared path so it works regardless of runtime user.
RUN mkdir -p /opt/duckdb/extensions && \
    python -c "import duckdb; c=duckdb.connect(config={'extension_directory':'/opt/duckdb/extensions'}); c.install_extension('httpfs'); c.install_extension('aws'); c.close()"

RUN useradd -u 5000 app -m && chown app:app /code && chown app:app /code/uploads
USER app:app

CMD ["./bin/start.sh"]
