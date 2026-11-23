#!/bin/sh
set -eu
umask 077
source_dir=$(cd "$(dirname "$0")" && pwd)
bundle=${1:?Pass a new path ending in .app}
case "$bundle" in *.app) ;; *) exit 64 ;; esac
if [ -e "$bundle" ]; then
    echo 'The destination already exists.' >&2
    exit 1
fi
mkdir -p "$bundle/Contents/MacOS"
cp "$source_dir/Info.plist" "$bundle/Contents/Info.plist"
xcrun swiftc -O "$source_dir/main.swift" -o "$bundle/Contents/MacOS/DockerSettingsReader"
codesign --force --sign - --timestamp=none --options runtime \
    --entitlements "$source_dir/entitlements.plist" "$bundle"
codesign --verify --strict "$bundle"
