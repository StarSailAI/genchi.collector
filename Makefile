COMPOSE ?= docker-compose

.PHONY: install test lint validate up down review

install:
	python -m pip install -e packages/contracts -e packages/sdk -e services/controller -e services/worker -e plugins/builtin -e plugins/genchi -e services/normalizer -e services/product -e dashboard -e '.[dev]'

test:
	pytest -q

lint:
	ruff check .

validate:
	allfeeds-plugin validate --sources config/sources.yaml
	allfeeds-control config-validate --sources config/sources.yaml

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

review:
	$(COMPOSE) exec normalizer genchi-normalizer review list
