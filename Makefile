UA ?= $(SEC_USER_AGENT)

.PHONY: install dev run test clean
install:
	pip install -e .
dev:
	pip install -e ".[dev]"
run:
	python -m tickerlineage.pipeline --out data/ --user-agent "$(UA)"
test:
	python -m pytest -q
clean:
	rm -rf .cache .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
