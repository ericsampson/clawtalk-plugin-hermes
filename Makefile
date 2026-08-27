# Development helpers. HERMES points at a Hermes Agent checkout; the adapter
# and WebSocket tests need it importable and skip without it.
HERMES ?= ../hermes-agent
PYTHON ?= $(HERMES)/venv/bin/python

.PHONY: help test lint check install-dev link clean

help:
	@echo "make test    run the test suite"
	@echo "make lint    run ruff"
	@echo "make check   lint + test"
	@echo "make link    symlink this repo into ~/.hermes/plugins/clawtalk"

test:
	PYTHONPATH=$(HERMES) $(PYTHON) -m pytest tests

lint:
	$(PYTHON) -m ruff check .

check: lint test

install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

# The plugin dir must be named `clawtalk`: Hermes derives the plugin name,
# platform, and skill namespace from the folder name.
link:
	mkdir -p $(HOME)/.hermes/plugins
	ln -sfn $(CURDIR) $(HOME)/.hermes/plugins/clawtalk
	@echo "Linked. Now run: hermes plugins enable clawtalk"

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
