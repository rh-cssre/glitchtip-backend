FROM python:3.8
ENV PYTHONUNBUFFERED 1
ENV PORT 8080

RUN mkdir /code
WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

COPY . /code/

CMD ["./bin/start.sh"]
