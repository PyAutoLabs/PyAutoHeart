#!/usr/bin/env bash
_self="$(readlink -f "${BASH_SOURCE[0]}")"
_root="$(cd "$(dirname "$_self")/../.." && pwd)"
exec bash "$_root/heart/checks/repo_state.sh" "$@"
