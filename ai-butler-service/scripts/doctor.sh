#!/usr/bin/env bash
set -uo pipefail

failures=0

check_command() {
  local command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    echo "OK   $command_name: $(command -v "$command_name")"
  else
    echo "MISS $command_name"
    failures=$((failures + 1))
  fi
}

check_command git
check_command docker
check_command uv
check_command python3

if command -v python3 >/dev/null 2>&1; then
  python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if [[ "$python_version" == "3.13" ]]; then
    echo "OK   Python $python_version"
  else
    echo "MISS Python 3.13 is required; current python3 is $python_version"
    failures=$((failures + 1))
  fi
fi

if docker info >/dev/null 2>&1; then
  echo "OK   Docker engine is running"
else
  echo "MISS Docker engine is not running; start Docker Desktop 4.24.2"
  failures=$((failures + 1))
fi

if docker compose version >/dev/null 2>&1; then
  echo "OK   Docker Compose is available"
else
  echo "MISS Docker Compose is unavailable"
  failures=$((failures + 1))
fi

if [[ ! -f .env.local ]]; then
  echo "INFO .env.local is absent; copy .env.example before local development"
fi

for port in 8000 5432 6333 6334; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "INFO port $port is already in use"
  else
    echo "OK   port $port is available"
  fi
done

available_kb=$(df -Pk . | awk 'NR==2 {print $4}')
if [[ "$available_kb" -lt 10485760 ]]; then
  echo "MISS less than 10 GiB disk space is available"
  failures=$((failures + 1))
else
  echo "OK   at least 10 GiB disk space is available"
fi

exit "$failures"
