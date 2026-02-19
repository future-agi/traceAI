#!/bin/bash
# Run local backend for E2E testing

# cd path/to/backend

# Override Docker hostnames with localhost
export PG_HOST=localhost
export PG_PORT=5432
export PG_DB=tfc
export PG_USER=user
export PG_PASSWORD=password

export REDIS_URL=redis://localhost:6379/0
export REDIS_LOCK_URL=redis://localhost:6379/1
export REDIS_STATE_URL=redis://localhost:6379/2

export CH_HOST=localhost
export CH_PORT=9000
export CH_DATABASE=default
export CH_USERNAME=default
export CH_PASSWORD=
export CH_ENABLED=true

export CELERY_BROKER_URL=amqp://user:password@localhost:5672//

export DJANGO_SETTINGS_MODULE=tfc.settings.settings
export ENV_TYPE=local
export FAST_STARTUP=true

# Run Django dev server
.venv/bin/python manage.py runserver 0.0.0.0:8000
