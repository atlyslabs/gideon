FROM python:3.13-alpine

ENV POETRY_VERSION=1.8.3 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# WeasyPrint runtime deps (used by tools/pdf_tool.py for HTML/Markdown to PDF).
# pango + cairo are the rendering engines; ttf-dejavu seeds the font cache so
# default fonts resolve when the HTML doesn't specify @font-face.
RUN apk add --no-cache pango cairo gdk-pixbuf libffi fontconfig ttf-dejavu

RUN pip install poetry==$POETRY_VERSION

WORKDIR /app

# Dependency manifest first so the install layer caches across code edits.
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --without dev

COPY . /app

EXPOSE 8080

CMD ["sh", "-c", "poetry run uvicorn main:app --host 0.0.0.0 --port 8080 --workers ${UVICORN_WORKERS:-1}"]
