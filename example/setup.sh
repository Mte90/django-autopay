#!/usr/bin/env bash
# Bootstrap the demo: create db.sqlite3 (gitignored) and a demo superuser.
set -euo pipefail
cd "$(dirname "$0")/.."

export DJANGO_SUPERUSER_USERNAME="${DEMO_USER:-demo}"
export DJANGO_SUPERUSER_EMAIL="demo@example.com"
export DJANGO_SUPERUSER_PASSWORD="${DEMO_PASSWORD:-demo}"

uv run python example/manage.py migrate --noinput
uv run python example/manage.py createsuperuser --noinput \
    || echo "Demo user already exists — skipping."

echo
echo "Demo ready. Start it with:"
echo "  uv run python example/manage.py runserver"
echo "Log in at http://localhost:8000/accounts/login/ (demo / demo)."
