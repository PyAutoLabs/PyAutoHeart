#!/usr/bin/env bash
# Compatibility wrapper for the former Pulse shell common helpers.
_self="$(readlink -f "${BASH_SOURCE[0]}")"
_root="$(cd "$(dirname "$_self")/.." && pwd)"
source "$_root/heart/_common.sh"
