#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<'SQL'
\getenv studio_password STUDIO_DATABASE_PASSWORD
CREATE ROLE studio LOGIN PASSWORD :'studio_password';
ALTER DATABASE studio OWNER TO studio;
SQL
