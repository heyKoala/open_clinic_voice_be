#!/usr/bin/env bash
# ManageOPD backend — one-command dev setup for Linux / macOS
# Usage: bash scripts/setup.sh
set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

echo "==> Creating virtual environment in ${VENV_DIR}/"
"$PYTHON" -m venv "$VENV_DIR"

echo "==> Upgrading pip"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip

echo "==> Installing pinned dependencies from requirements.txt"
"$VENV_DIR/bin/python" -m pip install --quiet -r requirements.txt

if [ ! -f "$ENV_FILE" ]; then
    echo "==> Copying ${ENV_EXAMPLE} → ${ENV_FILE} (edit before running the server)"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
else
    echo "==> ${ENV_FILE} already exists — skipping copy"
fi

echo "==> Running Django system check"
"$VENV_DIR/bin/python" manage.py check

echo "==> Applying migrations"
"$VENV_DIR/bin/python" manage.py migrate --run-syncdb

echo "==> Running test suite"
"$VENV_DIR/bin/python" -m pytest

echo ""
echo "✅  Setup complete. Start the dev server with:"
echo "    .venv/bin/python manage.py runserver"
