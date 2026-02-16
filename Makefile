PYTHON ?= python3
VENV ?= .venv

PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: venv install desktop desktop-test protocol

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

desktop:
	PYTHONPATH=src $(PY) -m agent_service.desktop

desktop-test:
	PYTHONPATH=src $(PY) -m agent_service.desktop --test

protocol:
	PYTHONPATH=src $(PY) -m agent_service.protocol_server
