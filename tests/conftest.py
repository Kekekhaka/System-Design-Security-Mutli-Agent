from src.core.config import settings

# Test-only values; real deployments must configure their own secret, and dev
# auth is off by default.
settings.JWT_SECRET = "test-only-secret-" + "x" * 32
settings.ENABLE_DEV_AUTH = True
