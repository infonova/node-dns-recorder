FROM python:3.9-slim

WORKDIR /node-dns-recorder

RUN pip install poetry && poetry config virtualenvs.create false

COPY main.py poetry.lock pyproject.toml ./

RUN poetry install --no-dev

CMD [ "kopf", "run", "main.py" ]