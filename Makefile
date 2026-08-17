.PHONY: help install test lint smoke backtest dry-run bootstrap digest finder clean

help:
	@echo "JobOS targets:"
	@echo "  make install    install runtime + dev dependencies"
	@echo "  make test       unit tests (no network)"
	@echo "  make lint       ruff + mypy"
	@echo "  make smoke      LIVE: hit every board in companies.yaml, print a table"
	@echo "  make backtest   replay 30 days of feed data, show ping volume/day"
	@echo "  make dry-run    full pipeline, print the notification instead of sending"
	@echo "  make bootstrap  record everything as seen, send nothing (first run)"
	@echo "  make digest     send the queued daily digest"
	@echo "  make finder     discover new companies (add --open-pr to raise a PR)"

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest tests/ -q

lint:
	ruff check joboS tests
	mypy joboS

smoke:
	python -m joboS.smoke

backtest:
	python -m joboS.backtest --days 30

dry-run:
	python -m joboS.poll --dry-run

bootstrap:
	python -m joboS.poll --bootstrap

digest:
	python -m joboS.poll --digest

finder:
	python -m joboS.finder

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__ */__pycache__ */*/__pycache__
