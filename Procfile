release: uv run python manage.py migrate --noinput
web: uv run python -m gunicorn web.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --access-logfile -
trader: uv run python -m trader
tgbot: uv run python -m tgbot
