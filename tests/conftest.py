import os

# Provide a default so get_settings() doesn't fail during test collection.
# Tests that construct Settings explicitly pass their own values.
os.environ.setdefault("THEO_DATABASE_URL", "postgresql://theo:test@localhost:5432/theo")
