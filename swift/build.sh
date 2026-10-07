#!/bin/bash
# Builds bench-store from the DayDream 0.1.6 source (tag in SOURCE_COMMIT) with the release typing flags.
# Needs Xcode or the Swift command line tools on an Apple silicon Mac (macOS 15+).
set -eo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); S="$HERE/build"; mkdir -p "$S/tmp" "$S/modcache"
if [ ! -d "$HERE/daydream-0.1.6-src" ]; then
  git clone --quiet --depth 1 --branch v0.1.6 https://github.com/getnorthlight/daydream.git "$HERE/daydream-0.1.6-src"
fi
export TMPDIR="$S/tmp/" CLANG_MODULE_CACHE_PATH="$S/modcache" SWIFT_MODULECACHE_PATH="$S/modcache"
FLAGS=(-Xswiftc -DDAYDREAM_OWNER_TYPING -Xswiftc -DDAYDREAM_CHROME_TYPING)   # as scripts/developer-id-release.py passes
(cd "$HERE/daydream-0.1.6-src" && swift build --jobs 6 --target MemoryCore --disable-automatic-resolution --scratch-path "$S/owner" "${FLAGS[@]}")
B="$S/owner/arm64-apple-macosx/debug"
swiftc -module-cache-path "$S/modcache" -I "$B/Modules" -I "$HERE/daydream-0.1.6-src/Sources/CSQLite" -D DAYDREAM_OWNER_TYPING -D DAYDREAM_CHROME_TYPING \
  "$HERE/bench-store.swift" "$B/MemoryCore.build/"*.o "$B/HistoryCore.build/"*.o "$B/PrivacyPolicy.build/"*.o -o "$HERE/bench-store" 2>&1 | grep -v '^ld: warning' || true
ls -la "$HERE/bench-store"
