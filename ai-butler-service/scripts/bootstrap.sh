#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it with Homebrew before bootstrapping." >&2
  exit 1
fi

if [[ ! -f .env.local ]]; then
  cp .env.example .env.local
  echo "created .env.local from .env.example"
fi

uv sync --all-groups --frozen

