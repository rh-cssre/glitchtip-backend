COMPOSE = docker compose -f compose.yml
COMPOSE_EXEC = $(COMPOSE) exec -it
COMPOSE_RUN = $(COMPOSE) run -it --rm

bash: 					# Run bash inside `web` container
	$(COMPOSE_EXEC) web bash

build:					# Build containers and pull images
	$(COMPOSE) pull
	$(COMPOSE) build

build-no-cache:			# Build containers without using cache
	$(COMPOSE) pull
	$(COMPOSE) build --no-cache

clean: stop				# Stop and clean orphan containers
	$(COMPOSE) down -v --remove-orphans

dbshell: 				# Connect to database shell using `web` container
	$(COMPOSE_EXEC) postgres psql postgres://postgres:postgres@127.0.0.1:5432/postgres

help:					# List all make commands
	@awk -F ':.*#' '/^[a-zA-Z_-]+:.*?#/ { printf "\033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST) | sort

kill:					# Force stop (kill) and remove containers
	$(COMPOSE) kill
	$(COMPOSE) rm --force

lint:
	$(COMPOSE_RUN) ruff check glitchtip/ apps/ --fix

lint-check:
	$(COMPOSE_RUN) ruff check glitchtip/ apps/

# TODO: needs to add `IS_LOAD_TEST=true` env var
locust-start:			# Start all containers in background (Locust mode)
	$(COMPOSE) -f compose.locust.yml up

locust-stop:			# Stop all containers (Locust mode)
	$(COMPOSE) -f compose.locust.yml down

locust-restart: locust-stop locust-start		# Stop all containers and start all containers in background (Locust mode)

logs:					# Show all containers' logs (follow)
	$(COMPOSE) logs -tf

migrate:				# Execute Django migrations inside `web` container
	$(COMPOSE_RUN) web python manage.py migrate

migrations:				# Execute `makemigrations` inside `web` container
	$(COMPOSE_RUN) web python manage.py makemigrations

partman-start:			# Start all containers in background (partman mode)
	$(COMPOSE) -f compose.part.yml up

partman-stop:			# Stop all containers (partman mode)
	$(COMPOSE) -f compose.part.yml down

partman-restart: locust-stop locust-start		# Stop all containers and start all containers in background (partman mode)

restart: stop start		# Stop all containers and start all containers in background

shell:					# Execute Django shell inside `web` container
	$(COMPOSE_EXEC) web python manage.py shell

start:					# Start all containers in background
	$(COMPOSE) up -d

stop:					# Stop all containers
	$(COMPOSE) down

test:					# Execute `pytest` and coverage report inside `web` container
	$(COMPOSE_RUN) web python manage.py test

.PHONY: bash build build-no-cache clean dbshell help kill lint lint-check locust-start locust-stop locust-restart logs migrate migrations partman-start partman-stop partman-restart restart shell start stop test
