#!/usr/bin/env bash
# Compatibility wrapper for the former Pulse tick path.
_self="$(readlink -f "${BASH_SOURCE[0]}")"
_root="$(cd "$(dirname "$_self")/.." && pwd)"
exec bash "$_root/heart/tick.sh" "$@"
