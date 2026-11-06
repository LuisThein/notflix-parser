#!/bin/sh
# Runs the unit tests inside the service image, so the dependencies match
# production. Builds the image first. No network access, no .env needed.
# The sources are copied in with `docker cp` instead of a bind mount,
# because Colima does not share every host folder with its VM.
set -eu

cd "$(dirname "$0")/.."

name="notflix-test-$$"
docker compose build --quiet parser
docker create --name "$name" --network none -e PYTHONDONTWRITEBYTECODE=1 -w /tmp \
    notflix-parser:local python -m unittest discover -s tests -v >/dev/null
trap 'docker rm -f "$name" >/dev/null' EXIT
tar -cf - notflix-parser.py tests | docker cp - "$name:/tmp"
docker start -a "$name"
