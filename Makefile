.PHONY: install lint fmt test tf-fmt tf-validate package-api package-dispatcher package-worker package-finalizer

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

# Assemble .build/api for `terraform apply`: function code plus Linux/py3.12
# wheels so the zip works inside the Lambda runtime (no Docker needed).
package-api:
	rm -rf .build
	mkdir -p .build/api
	cp -r src/. .build/api/
	python -m pip install -r requirements-api.txt -t .build/api -q \
		--platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12

# Dispatcher and worker run on stdlib + the Lambda-provided boto3.
package-dispatcher:
	rm -rf .build/dispatcher
	mkdir -p .build/dispatcher
	cp -r src/. .build/dispatcher/

package-worker:
	rm -rf .build/worker
	mkdir -p .build/worker
	cp -r src/. .build/worker/

package-finalizer:
	rm -rf .build/finalizer
	mkdir -p .build/finalizer
	cp -r src/. .build/finalizer/
