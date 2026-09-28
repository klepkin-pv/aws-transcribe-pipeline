.PHONY: install lint fmt test tf-fmt tf-validate

install:
	python -m pip install "pytest>=8.0" "ruff>=0.6"

lint:
	ruff check src tests

fmt:
	ruff format src tests

test:
	pytest

tf-fmt:
	terraform -chdir=infra fmt -recursive

tf-validate:
	terraform -chdir=infra init -backend=false
	terraform -chdir=infra validate
