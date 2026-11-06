#!/bin/bash
set -euo pipefail
umask 077
source_dir="$(cd "$(dirname "$0")" && pwd)"
build_dir="$source_dir/.build"
mkdir -p "$build_dir"
/usr/bin/clang -fobjc-arc -O2 -Wall -Wextra -Werror -Wno-deprecated-declarations \
  -framework Cocoa -framework ApplicationServices -framework Security \
  "$source_dir/main.m" "$source_dir/policy.m" "$source_dir/vendor-hosts.m" -o "$build_dir/CodexMemoryMaintenance"
/usr/bin/clang -fobjc-arc -O2 -Wall -Wextra -Werror -framework Foundation \
  "$source_dir/tests.m" "$source_dir/policy.m" -o "$build_dir/tests"
/usr/bin/clang -O2 -Wall -Wextra -Werror "$source_dir/terminal-state-tests.c" -o "$build_dir/terminal-state-tests"
"$build_dir/terminal-state-tests"
"$build_dir/tests"
/usr/bin/clang -fobjc-arc -O2 -Wall -Wextra -Werror -framework Foundation -framework Security \
  "$source_dir/vendor-hosts-tests.m" -o "$build_dir/vendor-tests"
"$build_dir/vendor-tests"
/usr/bin/python3 -B "$source_dir/entrypoint-fixtures.py"
if [[ "${1:---build-only}" == "--build-only" ]]; then exit 0; fi
if [[ "${1:-}" != "--install" ]]; then echo 'Use --build-only or --install' >&2; exit 2; fi
# Install only a fresh, reviewed identity. Never silently replace a TCC-approved binary/config.
task_private="$HOME/.config/codex-memory-maintenance"
task_app="$HOME/Applications/Codex Memory Maintenance.app"
if [[ -e "$task_app" || -L "$task_app" || -e "$task_private" || -L "$task_private" ]]; then
  echo 'Existing installation: preserve it and review an explicit upgrade; nothing installed.' >&2; exit 2
fi
mkdir -m 700 "$task_private"
mkdir -p "$HOME/Applications"
stage_app="$task_private/staged/Codex Memory Maintenance.app"
mkdir -p "$stage_app/Contents/MacOS"
cp "$build_dir/CodexMemoryMaintenance" "$stage_app/Contents/MacOS/CodexMemoryMaintenance"
/usr/bin/python3 - "$source_dir" "$task_private" "$stage_app" <<'PY'
import hashlib, json, os, pathlib, plistlib, re, subprocess, sys
source, private, stage = map(pathlib.Path, sys.argv[1:])
cfg = json.loads((source/'config.example.json').read_text())
target = pathlib.Path(cfg['target_bundle'])
info = plistlib.loads((target/'Contents/Info.plist').read_bytes())
for key, setting in [('CFBundleIdentifier','target_bundle_id'),('CFBundleVersion','target_build'),('CFBundleShortVersionString','target_version')]:
    if info[key] != cfg[setting]:
        raise SystemExit('Reviewed target version changed; requalification required')
subprocess.run(['/usr/bin/codesign','--verify','--strict',str(target)], check=True, capture_output=True)
sign = subprocess.run(['/usr/bin/codesign','-d','--verbose=4',str(target)], check=True, capture_output=True,text=True).stderr
for field, setting in [('TeamIdentifier','target_team_id'),('CDHash','target_cdhash')]:
    if re.search(r'^'+field+r'=(.*)$', sign, re.M).group(1) != cfg[setting]:
        raise SystemExit('Reviewed target code identity changed; requalification required')
package = target/'Contents/Resources/app.asar'
before = package.stat()
h = hashlib.sha256()
with package.open('rb') as stream:
    while block := stream.read(65536): h.update(block)
after = package.stat()
if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
    raise SystemExit('Target changed while hashing; no activation')
cfg['target_asar_sha256'] = h.hexdigest()
(private/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
(private/'state.json').write_text(json.dumps({'schema_version':1},indent=2)+'\n')
helper = {'CFBundleIdentifier':'com.zack.codex-memory-maintenance','CFBundleExecutable':'CodexMemoryMaintenance',
          'CFBundleName':'Codex Memory Maintenance','CFBundleDisplayName':'Codex Memory Maintenance',
          'CFBundleVersion':'1','CFBundleShortVersionString':'1.0','CFBundlePackageType':'APPL',
          'LSUIElement':True,'NSHighResolutionCapable':True,'LSMinimumSystemVersion':'13.0'}
(stage/'Contents/Info.plist').write_bytes(plistlib.dumps(helper))
template = plistlib.loads((source/'launchagent.plist.in').read_bytes())
template['ProgramArguments'][0] = template['ProgramArguments'][0].replace('__HOME__',str(pathlib.Path.home()))
(private/'com.zack.codex-memory-maintenance.plist').write_bytes(plistlib.dumps(template))
for name in ['config.json','state.json','com.zack.codex-memory-maintenance.plist']:
    (private/name).chmod(0o600)
PY
/usr/bin/codesign --force --sign - --identifier com.zack.codex-memory-maintenance "$stage_app"
/usr/bin/codesign --verify --strict "$stage_app"
mv "$stage_app" "$task_app"
rmdir "$task_private/staged"
"$task_app/Contents/MacOS/CodexMemoryMaintenance" --check-permissions
"$task_app/Contents/MacOS/CodexMemoryMaintenance" --dry-run
echo 'Installed disabled; LaunchAgent is only a private template. No quit, prompt, or launchctl load performed.'
