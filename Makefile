PYTHON ?= python3
VENV ?= .venv
HOST ?= 0.0.0.0
PORT ?= 8088

PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PY := $(VENV)/bin/python

.PHONY: venv install run desktop desktop-test

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run:
	$(UVICORN) agent_service.main:app --app-dir src --host $(HOST) --port $(PORT)

desktop:
	PYTHONPATH=src $(PY) -m agent_service.desktop

desktop-test:
	PYTHONPATH=src $(PY) -m agent_service.desktop --test
