FROM python:3.8-slim
ENV PYTHONUNBUFFERED=1 \
  PORT=8080 \
  POETRY_VERSION=1.0.3 \
  POETRY_VIRTUALENVS_CREATE=false

RUN mkdir /code
WORKDIR /code

RUN apt-get update && apt-get install -y gcc
RUN pip install "poetry==$POETRY_VERSION"
COPY poetry.lock pyproject.toml /code/
RUN poetry install --no-interaction --no-ansi

EXPOSE 8080

COPY . /code/

CMD ["./bin/start.sh"]
