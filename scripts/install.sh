#!/usr/bin/env bash
set -euo pipefail

REPO="christianwilkins/igrep"
VERSION="${VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

case "$(uname -s)" in
  Linux*) platform="linux" ;;
  Darwin*) platform="macos" ;;
  *)
    echo "Unsupported OS: $(uname -s)"
    exit 1
    ;;
esac

if [[ "$platform" == "linux" ]]; then
  asset="igrep-linux-amd64.tar.gz"
elif [[ "$platform" == "macos" ]]; then
  asset="igrep-macos-amd64.tar.gz"
fi

if [[ "$VERSION" == "latest" ]]; then
  download_url="https://github.com/${REPO}/releases/latest/download/${asset}"
else
  download_url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"
fi

archive_path="$TMP_DIR/$asset"
curl -fsSL "$download_url" -o "$archive_path"

tar -xzf "$archive_path" -C "$TMP_DIR"

mkdir -p "$INSTALL_DIR"
cp "$TMP_DIR/igrep" "$INSTALL_DIR/igrep"
chmod +x "$INSTALL_DIR/igrep"

echo "Installed igrep to $INSTALL_DIR/igrep"
"$INSTALL_DIR/igrep" --help >/dev/null
