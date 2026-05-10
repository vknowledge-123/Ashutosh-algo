#!/bin/sh
set -eu

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/bin/systemctl}"
DEFAULT_UNITS="${TRADING_STACK_UNITS:-trading.service}"

if [ "$#" -gt 0 ]; then
  exec "$SYSTEMCTL_BIN" restart --no-block "$@"
fi

# Intentionally rely on shell word-splitting so multiple units can be supplied
# through TRADING_STACK_UNITS, for example:
#   TRADING_STACK_UNITS="trading.service redis-server.service"
exec "$SYSTEMCTL_BIN" restart --no-block $DEFAULT_UNITS
