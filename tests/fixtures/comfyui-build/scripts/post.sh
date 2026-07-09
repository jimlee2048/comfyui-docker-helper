#!/usr/bin/env bash
set -eu
{
  printf 'post.sh cwd=%s\n' "$PWD"
  printf 'post.sh WORKSPACE=%s\n' "$WORKSPACE"
  printf 'post.sh COMFYUI_PATH=%s\n' "$COMFYUI_PATH"
  printf 'post.sh VIRTUAL_ENV=%s\n' "$VIRTUAL_ENV"
} >> "$COMFYUI_PATH/cdh-smoke-hook-events.log"
