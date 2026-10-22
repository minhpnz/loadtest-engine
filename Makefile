SHELL := /bin/bash
PYTHON ?= .venv/bin/python
BOOTSTRAP_PYTHON ?= python3
SERVICE ?= payment-service
KUBE_CONTEXT ?= kind-loadtest-engine
OUTPUT ?= artifacts
TF_ENV ?= nonprod-apse1
TF_ROOT := infra/live/$(TF_ENV)

.PHONY: setup test validate-local render-local local-up run-local local-down install-platform tf-check demo

setup:
	$(BOOTSTRAP_PYTHON) -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; set BOOTSTRAP_PYTHON=python3.12"'
	$(BOOTSTRAP_PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r infra/requirements.txt

test:
	$(PYTHON) -m unittest discover -s infra/tests -v

validate-local:
	$(PYTHON) -m infra.engine validate --local --context "$(KUBE_CONTEXT)" --service "$(SERVICE)"

render-local:
	@mkdir -p artifacts
	$(PYTHON) -m infra.engine render --local --context "$(KUBE_CONTEXT)" --service "$(SERVICE)" > artifacts/manifests.json

local-up:
	$(PYTHON) -m infra.scripts.local_up

run-local:
	$(PYTHON) -m infra.engine run --local --context "$(KUBE_CONTEXT)" --service "$(SERVICE)" --output "$(OUTPUT)"

local-down:
	kind delete cluster --name loadtest-engine

install-platform:
	$(PYTHON) -m infra.engine install --context "$(KUBE_CONTEXT)" --engine-image "$(ENGINE_IMAGE)"

tf-check:
	terraform -chdir="$(TF_ROOT)" fmt -check
	terraform -chdir=infra/modules fmt -check -recursive
	terraform -chdir="$(TF_ROOT)" validate

demo: validate-local run-local
	@cat "$(OUTPUT)/report.json"
	kubectl --context "$(KUBE_CONTEXT)" get namespaces -l loadtest.engine/managed=true
