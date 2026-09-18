#!/usr/bin/env bash
# Assert the release tag matches the package version, so a `vX.Y.Z` tag can never
# publish an image built from a different version. Called with the tag as $1
# (e.g. "v0.1.0"). The version is dynamic in pyproject.toml — hatch reads it from
# vgi_hackernews/__init__.py — so that is where this looks. Dependency-free.
set -euo pipefail

tag="${1:?usage: check-version.sh <tag>}"
want="${tag#v}"  # strip a leading 'v'
have="$(sed -nE 's/^__version__ = "([^"]+)".*/\1/p' vgi_hackernews/__init__.py)"

if [ -z "$have" ]; then
  echo "No __version__ found in vgi_hackernews/__init__.py" >&2
  exit 1
fi
if [ "$want" != "$have" ]; then
  echo "Version mismatch: tag ${tag} (-> ${want}) != vgi_hackernews.__version__ ${have}" >&2
  exit 1
fi
echo "Version OK: vgi_hackernews ${have} matches tag ${tag}"
