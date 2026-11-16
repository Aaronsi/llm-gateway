.PHONY: install run test lint docker

install:
	uv sync

run:
	GATEWAY_CONFIG=config.yaml uvicorn --factory app.app:create_app --reload --port 8000

test:
	uv run pytest -q

lint:
	uv run ruff check .

docker:
	docker compose up --build
