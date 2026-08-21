# Alembic migrations

Production database schema is managed with Alembic.

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe_change"
```

The container entrypoint runs `alembic upgrade head` before starting the API.
