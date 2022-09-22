FROM python:3.10 as build-python
ARG IS_CI
ENV PYTHONUNBUFFERED=1 \
  PORT=8080 \
  POETRY_VIRTUALENVS_CREATE=false \
  POETRY_HOME=/opt/poetry \
  PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /code
RUN curl -sSL https://install.python-poetry.org | python3 -
COPY poetry.lock pyproject.toml /code/
RUN $POETRY_HOME/bin/poetry install --no-interaction --no-ansi $(test "$IS_CI" = "True" && echo "--no-dev")

FROM python:3.10-slim
ARG GLITCHTIP_VERSION=local
ENV GLITCHTIP_VERSION ${GLITCHTIP_VERSION}
ENV PYTHONUNBUFFERED=1 \
  PORT=8080

RUN apt-get update && apt-get install -y libxml2 libpq5 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY --from=build-python /usr/local/lib/python3.10/site-packages/ /usr/local/lib/python3.10/site-packages/
COPY --from=build-python /usr/local/bin/ /usr/local/bin/

EXPOSE 8080

COPY . /code/
ARG COLLECT_STATIC
RUN if [ "$COLLECT_STATIC" != "" ] ; then SECRET_KEY=ci ./manage.py collectstatic --noinput; fi

RUN useradd -u 5000 app && chown app:app /code && chown app:app /code/uploads
USER app:app

CMD ["./bin/start.sh"]
