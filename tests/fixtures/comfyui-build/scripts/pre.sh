#!/usr/bin/env bash
set -eu
{
  printf 'pre.sh cwd=%s\n' "$PWD"
  printf 'pre.sh WORKSPACE=%s\n' "$WORKSPACE"
  printf 'pre.sh COMFYUI_PATH=%s\n' "$COMFYUI_PATH"
  printf 'pre.sh VIRTUAL_ENV=%s\n' "$VIRTUAL_ENV"
} >> "$COMFYUI_PATH/cdh-smoke-hook-evidence.log"
