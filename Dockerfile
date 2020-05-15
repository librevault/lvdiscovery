FROM python:3.8.3-alpine3.11

WORKDIR /srv
COPY poetry.lock pyproject.toml ./

RUN apk add --virtual .build-deps build-base libffi-dev libressl-dev \
&& pip3 install poetry==1.0.5 \
&& python3 -m venv .venv \
&& poetry install \
&& apk del .build-deps

COPY . ./

EXPOSE 8080
CMD .venv/bin/hypercorn -b "[::]:8080" services.tracker:app
