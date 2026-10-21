import os

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("REMNAWAVE_URL", "http://localhost")
os.environ.setdefault("REMNAWAVE_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_IDS", "[]")
