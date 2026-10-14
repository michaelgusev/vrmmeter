#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Configure group
if ! getent group vrmmeter >/dev/null 2>&1; then
    groupadd -g "$PGID" vrmmeter
else
    groupmod -o -g "$PGID" vrmmeter >/dev/null 2>&1 || true
fi

# Configure user
if ! id -u vrmmeter >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -d /app -s /sbin/nologin vrmmeter
else
    usermod -o -u "$PUID" -g "$PGID" vrmmeter >/dev/null 2>&1 || true
fi

# Ensure data directory exists and has correct ownership
mkdir -p /app/data
chown -R vrmmeter:vrmmeter /app/data

# If running as root, drop privileges and execute command as vrmmeter
if [ "$(id -u)" = '0' ]; then
    exec gosu vrmmeter "$@"
fi

exec "$@"