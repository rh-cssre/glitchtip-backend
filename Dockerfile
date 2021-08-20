FROM python:3.9 as build-python
ARG IS_CI
ENV PYTHONUNBUFFERED=1 \
  PORT=8080 \
  POETRY_VIRTUALENVS_CREATE=false \
  PIP_DISABLE_PIP_VERSION_CHECK=on

RUN pip install poetry
WORKDIR /code
COPY poetry.lock pyproject.toml /code/
RUN poetry install --no-interaction --no-ansi $(test "$IS_CI" = "True" && echo "--no-dev")

FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1 \
  PORT=8080

RUN apt-get update && apt-get install -y libxml2 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY --from=build-python /usr/local/lib/python3.9/site-packages/ /usr/local/lib/python3.9/site-packages/
COPY --from=build-python /usr/local/bin/ /usr/local/bin/

EXPOSE 8080

COPY . /code/

CMD ["./bin/start.sh"]
