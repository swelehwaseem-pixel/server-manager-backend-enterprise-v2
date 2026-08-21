import os


def test_database_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")

    # Import is intentionally delayed because settings are instantiated at module load.
    import importlib
    import app.config as config
    importlib.reload(config)

    assert config.settings.database_url.startswith("postgresql+asyncpg://")
