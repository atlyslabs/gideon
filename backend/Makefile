.PHONY: run install test lint

install:
	poetry install

run:
	poetry run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

test:
	poetry run pytest tests/ -v --cov=. --cov-report=term-missing

lint:
	poetry run ruff check .
