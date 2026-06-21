# OrcheStack dev-bd8f81c — local install bundle

This bundle was built locally (not from a GitHub Release). The runtime
files are identical to what a tagged release produces, but the bundle
was assembled on the developer's machine via `scripts/build-bundle.sh`.

## Install

```sh
cp .env.example .env
$EDITOR .env                  # set ORCHESTACK_DB_PASSWORD
docker compose up -d
```

Visit http://localhost and sign up.

## Updating

Get a fresh tarball from the developer (or once the repo goes public, from
https://github.com/tripleaceme/orchestack-public/releases/latest) and replace the
files in this directory. Keep your `.env` — never overwrite it.
