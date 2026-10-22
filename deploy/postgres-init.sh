#!/bin/sh
# PostgreSQL's image runs this once, when its data volume is new (compose.yaml mounts it in
# /docker-entrypoint-initdb.d/): it makes the role the Latet instances connect as, latet, which is no
# superuser and owns the database latet, with the password of the latet-db-password secret. The image's
# own superuser, postgres, keeps the password of latet-db-admin-password, which no instance is given: a
# compromised instance cannot run a program in this container (COPY ... PROGRAM needs a superuser).
# The password is hexadecimal (openssl rand -hex 32): it reaches psql on its standard input, never as an
# argument of a command.
set -e
password=
IFS= read -r password < /run/secrets/latet-db-password || test -n "$password"
case "$password" in
  *[!0-9a-fA-F]* | '')
    echo "latet-db-password must be hexadecimal, as openssl rand -hex 32 makes it" >&2
    exit 1
    ;;
esac
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<EOSQL
CREATE ROLE latet LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '$password';
CREATE DATABASE latet OWNER latet;
EOSQL
unset password
