#!/bin/bash
# Build the Unraid plugin: a Slackware-style .txz payload plus the .plg
# descriptor that installs it.
#
#   ./plugin/build.sh                 build with the version in plugin/version
#   ./plugin/build.sh 2026.12.01      build a specific version
#
# Output (in plugin/dist/):
#   mnasx1-subscreen-<version>-x86_64-1.txz   payload
#   mnasx1-subscreen.plg                      installer descriptor
#
# Both files are meant to be uploaded together: the .plg must be reachable at
# the URL in its pluginURL attribute, and the .txz must be attached to the
# GitHub release tagged with the same version.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DIST="$HERE/dist"
VERSION=${1:-$(cat "$HERE/version")}
NAME=mnasx1-subscreen
PKG="$NAME-$VERSION-x86_64-1.txz"

command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }

echo "==> staging $NAME $VERSION"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

EMHTTP="$STAGE/usr/local/emhttp/plugins/$NAME"
LIBDIR="$STAGE/usr/local/lib/$NAME"
mkdir -p "$EMHTTP" "$LIBDIR"

cp -a "$HERE/src/usr/local/emhttp/plugins/$NAME/." "$EMHTTP/"

# The daemon itself. Two files only: no packaging of tests or docs.
install -m 0644 "$ROOT/subscreen_daemon.py" "$LIBDIR/subscreen_daemon.py"
install -m 0644 "$ROOT/subscreen_protocol.py" "$LIBDIR/subscreen_protocol.py"
printf '%s\n' "$VERSION" > "$LIBDIR/VERSION"

# Modes matter: the payload is extracted onto the RAM rootfs, but the staging
# tree may come from a filesystem (or a checkout) with different defaults.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -not -path '*/scripts/*' -not -path '*/event/*' \
     -exec chmod 644 {} +
find "$EMHTTP/scripts" "$EMHTTP/event" -type f -exec chmod 755 {} +

# A .page or .sh with CRLF silently breaks on Linux; normalise before packing.
find "$STAGE" -type f \( -name '*.page' -o -name '*.sh' -o -name '*.php' \
     -o -name '*.cfg' -o -name 'started' -o -name 'stopping' \) \
     -exec sed -i 's/\r$//' {} +

echo "==> packing $PKG"
mkdir -p "$DIST"
rm -f "$DIST/$PKG"
( cd "$STAGE" && tar --owner=0 --group=0 --numeric-owner -cJf "$DIST/$PKG" . )

SHA256=$(sha256sum "$DIST/$PKG" | cut -d' ' -f1)

# Keep a copy on the flash drive path the plugin expects, so a machine that has
# already downloaded the payload keeps working offline.
mkdir -p "$DIST/flash"
cp -f "$DIST/$PKG" "$DIST/flash/$PKG"

echo "==> rendering .plg"
sed -e "s/__VERSION__/$VERSION/g" \
    -e "s/__SHA256__/$SHA256/g" \
    "$HERE/$NAME.plg.in" > "$DIST/$NAME.plg"

# The descriptor must also live at the repo path its own pluginURL points at,
# so "Install Plugin" by URL always sees the version that matches the release.
cp -f "$DIST/$NAME.plg" "$HERE/$NAME.plg"

echo
echo "payload : $DIST/$PKG"
echo "sha256  : $SHA256"
echo "plugin  : $DIST/$NAME.plg"
echo
echo "Publish:"
echo "  gh release create $VERSION \"$DIST/$PKG\" --title \"$VERSION\" --notes-file -"
echo "  git add plugin/mnasx1-subscreen.plg && git commit -m \"plugin $VERSION\" && git push"
