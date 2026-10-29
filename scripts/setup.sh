#!/bin/bash
# Maintainer setup: activates the Git hooks in .githooks/ (Conventional Commits
# check, changelog in the Obsidian vault). Not needed to build or contribute;
# without the wiki settings the changelog hooks do nothing.
#
#   scripts/setup.sh VAULT-PATH PROJECT-FOLDER PREFIX
#   e.g. scripts/setup.sh "$HOME/Notes" "My Project" myproj
#   (changelog: $HOME/Notes/Projekte/My Project/Changelog.md, marker "%% myproj:commits %%")

set -eu
cd "$(git rev-parse --show-toplevel)"

if [ $# -lt 3 ]; then
    echo "usage: scripts/setup.sh VAULT-PATH PROJECT-FOLDER PREFIX" >&2
    exit 1
fi

case "$3" in
    *[!a-z0-9-]* | "") echo "setup: PREFIX may only contain a-z, 0-9 and '-'" >&2; exit 1 ;;
esac

git config core.hooksPath .githooks
git config wiki.vaultPath "$1"
git config wiki.project "$2"
git config wiki.prefix "$3"
chmod +x .githooks/* scripts/*.sh

changelog="$1/Projekte/$2/Changelog.md"
echo "Git hooks active (core.hooksPath=.githooks)."
echo "Changelog: $changelog"
[ -f "$changelog" ] || echo "setup: WARNING: changelog not found yet" >&2

email=$(git config --get user.email || true)
[ "$email" = "contact@luisthein.de" ] || echo "setup: WARNING: user.email is '$email', expected contact@luisthein.de" >&2
