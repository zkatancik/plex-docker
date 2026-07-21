#!/usr/bin/env sh
set -eu

echo
echo "-------------------------------------------------------------"
echo " Executing Tasks: $(date)"
echo "-------------------------------------------------------------"

if /app/recyclarr/recyclarr sync; then
  marker_dir=/config/state
  marker_tmp="${marker_dir}/last-sync-success.tmp"
  marker="${marker_dir}/last-sync-success"
  mkdir -p "$marker_dir"
  date +%s > "$marker_tmp"
  mv "$marker_tmp" "$marker"
fi
