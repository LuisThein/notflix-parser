#!/bin/sh
# Starts the spike in the background in a container of the service image,
# with the mailbox credentials from the repo's .env (mapped to GMX_USER and
# GMX_PASSWORD inside the container, never printed). Extra arguments are
# passed as environment, e.g. `spikes/gmx-idle/run.sh MODE=poll`.
# Follow it with `docker logs -f notflix-idle-spike`, stop it with
# `docker rm -f notflix-idle-spike`.
set -eu

cd "$(dirname "$0")/../.."

name=notflix-idle-spike
env_args=""
for arg in "$@"; do env_args="$env_args -e $arg"; done

docker rm -f "$name" >/dev/null 2>&1 || true
# shellcheck disable=SC2086
docker create --name "$name" --no-healthcheck --env-file .env $env_args -w /tmp notflix-parser:local \
    sh -c 'pip install --quiet --user imapclient==4.1.0 &&
           GMX_USER="$NOTFLIX_EMAIL" GMX_PASSWORD="$NOTFLIX_PASSWORT" \
           exec python -u /tmp/idle_spike.py' >/dev/null
docker cp spikes/gmx-idle/idle_spike.py "$name:/tmp/idle_spike.py"
docker start "$name" >/dev/null
echo "started $name"
