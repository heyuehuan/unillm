import os

# In-memory SQLite with StaticPool — fast, isolated, no file cleanup needed
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["UNILLM_JWT_SECRET"] = "test-secret"
