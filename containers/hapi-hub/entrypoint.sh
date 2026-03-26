#!/bin/bash
set -e
HAPI_MODE=${HAPI_MODE:-relay}
HAPI_LISTEN_HOST=${HAPI_LISTEN_HOST:-0.0.0.0}
HAPI_LISTEN_PORT=${HAPI_LISTEN_PORT:-3006}
export HAPI_LISTEN_HOST
export HAPI_LISTEN_PORT
case "$HAPI_MODE" in
  relay)
    echo "Starting hapi hub in relay mode (public relay server)"
    echo "Listening on ${HAPI_LISTEN_HOST}:${HAPI_LISTEN_PORT}"
    exec hapi hub --relay
    ;;
  local)
    echo "Starting hapi hub in local-only mode (no relay)"
    echo "Listening on ${HAPI_LISTEN_HOST}:${HAPI_LISTEN_PORT}"
    exec hapi hub --no-relay
    ;;
  custom)
    echo "Starting hapi hub in custom mode (user-configured relay)"
    echo "Listening on ${HAPI_LISTEN_HOST}:${HAPI_LISTEN_PORT}"
    echo "Expecting HAPI_PUBLIC_URL and HAPI_API_URL to be set externally"
    exec hapi hub
    ;;
  *)
    echo "ERROR: Invalid HAPI_MODE='${HAPI_MODE}'"
    echo "Valid modes: relay, local, custom"
    exit 1
    ;;
esac
