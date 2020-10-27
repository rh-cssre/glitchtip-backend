FROM python:3.8-slim
ARG IS_CI
ENV PYTHONUNBUFFERED=1 \
  PORT=8080 \
  POETRY_VERSION=1.1.4 \
  POETRY_VIRTUALENVS_CREATE=false \
  PIP_DISABLE_PIP_VERSION_CHECK=on

RUN mkdir /code
WORKDIR /code

RUN apt-get update && apt-get install -y gcc
RUN pip install "poetry==$POETRY_VERSION"
COPY poetry.lock pyproject.toml /code/
RUN poetry install --no-interaction --no-ansi $(test "$IS_CI" = "True" && echo "--no-dev")

EXPOSE 8080

COPY . /code/

CMD ["./bin/start.sh"]
