#!/bin/bash
set -e
# PostgreSQL has no CREATE DATABASE IF NOT EXISTS.
# Check pg_database first, create only if missing.
EXISTS=$(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -tAc "SELECT 1 FROM pg_database WHERE datname = 'theo_test'")
if [ "$EXISTS" != "1" ]; then
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "CREATE DATABASE theo_test OWNER $POSTGRES_USER"
fi
