# One obvious entry point for everything a contributor needs.
#
#   make dev     create .venv and install the project with its dev extras
#   make lint    ruff check, ruff format --check, English-only, link and version guards
#   make format  apply ruff format and ruff's safe fixes
#   make test    the unit suite (the live suite needs a real Immich, see CONTRIBUTING.md)
#   make image   build the container image locally
#   make docs    regenerate the generated documentation and the config JSON schema
#   make check   everything CI runs, in one go
#   make hooks   install the commit-msg hook that checks the subject convention

PY      := .venv/bin/python
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/immich_compressor/__init__.py)
IMAGE   ?= immich-compressor:$(VERSION)
PLATFORMS ?= linux/amd64,linux/arm64

.DEFAULT_GOAL := help
.PHONY: help dev lint language links version-check commits hooks format test test-live image image-multiarch docs docs-check compose-check clean

help:
	@sed -n 's/^#   //p' $(MAKEFILE_LIST) | head -8

$(PY):
	python3 -m venv .venv

dev: $(PY)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

lint: $(PY)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	./scripts/check-language.sh
	$(PY) scripts/check-links.py
	$(PY) scripts/check-workflows.py
	$(PY) scripts/version.py check

# The prose guards on their own.
language:
	./scripts/check-language.sh

links: $(PY)
	$(PY) scripts/check-links.py

# __version__, the CHANGELOG sections and the upgrading notes still agree.
version-check: $(PY)
	$(PY) scripts/version.py check

# The commit subjects on this branch. Empty, and so trivially green, on main.
commits: $(PY)
	$(PY) scripts/check-commits.py

# The same check before the commit exists. Fixing a subject afterwards means rewriting a
# branch that has already been pushed, and this project does not force-push — so the
# useful place for this check is the commit itself, not the pull request.
HOOK := $(shell git rev-parse --git-path hooks)/commit-msg

hooks:
	@printf '#!/bin/sh\nexec python3 "$$(git rev-parse --show-toplevel)/scripts/check-commits.py" --message "$$1"\n' > $(HOOK)
	@chmod +x $(HOOK)
	@echo "commit-msg hook installed at $(HOOK)"

format: $(PY)
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

# `-rs` for the same reason as test-live below: the encoder tests skip themselves when
# ffmpeg, ImageMagick or exiftool is missing, and a bare -q reports that as a green
# summary line. CI asserts the toolchain is present; locally the skip reasons are the
# only signal that a change to the still encoder was never executed.
test: $(PY)
	$(PY) -m pytest -m 'not live' -q -rs

# Needs E2E_IMMICH_URL and E2E_IMMICH_KEY pointing at a throwaway instance, plus
# E2E_IMMICH_EMAIL and E2E_IMMICH_PASSWORD for the sync-stream tests — an API key cannot
# open a sync session. `-rs` so a skip is visible instead of passing for green.
test-live: $(PY)
	$(PY) -m pytest -m live -q -rs

image:
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE) .

image-multiarch:
	docker buildx build --platform $(PLATFORMS) --build-arg VERSION=$(VERSION) -t $(IMAGE) .

docs: $(PY)
	$(PY) scripts/gen_docs.py

docs-check: $(PY)
	$(PY) scripts/gen_docs.py --check

# The base file refuses to render without the two secrets, which is the point. Feed it
# obvious placeholders so validation does not depend on a configured deployment.
COMPOSE_ENV := IMMICH_API_KEY=placeholder COMPRESSOR_TOKEN=placeholder RENDER_GID=993

compose-check:
	env $(COMPOSE_ENV) docker compose -f docker-compose.yaml config -q
	env $(COMPOSE_ENV) docker compose -f docker-compose.yaml -f docker-compose.build.yaml config -q
	env $(COMPOSE_ENV) docker compose -f docker-compose.yaml -f docker-compose.gpu.yaml config -q
	env $(COMPOSE_ENV) docker compose -f docker-compose.yaml -f docker-compose.gpu-nvidia.yaml config -q
	env $(COMPOSE_ENV) docker compose -f docker-compose.yaml -f docker-compose.override.example.yaml config -q

check: lint test docs-check compose-check commits

clean:
	rm -rf .venv .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
