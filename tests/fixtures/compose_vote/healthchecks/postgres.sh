#!/bin/bash
set -eo pipefail
host="$(hostname -i || echo 127.0.0.1)"
user="${POSTGRES_USER:-postgres}"
if select="$(psql -h "$host" -U "$user" --quiet --no-align --tuples-only -c "SELECT 1")" && [ "$select" = 1 ]; then exit 0; fi
exit 1
