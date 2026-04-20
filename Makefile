.PHONY: help install lint fmt typecheck test test-unit run dev check

help:
	@echo "Commandes disponibles :"
	@echo "  make install     Installe deps runtime + dev"
	@echo "  make lint        Run ruff"
	@echo "  make fmt         Auto-format (ruff + black)"
	@echo "  make typecheck   Run mypy"
	@echo "  make test        Run pytest (tous les tests)"
	@echo "  make test-unit   Run pytest (unit only, pas de DB)"
	@echo "  make check       lint + typecheck + test-unit (pre-commit)"
	@echo "  make run         uvicorn src.main:app (no reload)"
	@echo "  make dev         uvicorn src.main:app --reload"

install:
	pip install -r requirements.txt
	pip install ruff==0.14.4 black==25.11.0 mypy==1.18.2 pytest==8.4.2 pytest-cov==6.3.0

lint:
	ruff check src tests

fmt:
	ruff check --fix src tests
	ruff format src tests

typecheck:
	mypy src

test:
	pytest

test-unit:
	pytest -m unit

check: lint typecheck test-unit

run:
	uvicorn src.main:app --host 0.0.0.0 --port $${PORT:-8000}

dev:
	uvicorn src.main:app --reload
