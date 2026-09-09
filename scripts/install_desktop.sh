#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.local/share/block-n-pick/app"; BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"
mkdir -p "$DEST" "$BIN" "$APPS"; rm -rf "$DEST"/*; cp -a "$ROOT"/. "$DEST"/
cat > "$BIN/block-n-pick" <<EOF
#!/usr/bin/env bash
cd "$DEST"
exec python run_bnp.py "\$@"
EOF
cat > "$BIN/block-n-pick-gui" <<EOF
#!/usr/bin/env bash
cd "$DEST"
exec python -m bnp.gui "\$@"
EOF
chmod +x "$BIN/block-n-pick" "$BIN/block-n-pick-gui"
cat > "$APPS/block-n-pick.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Block-n-Pick
Comment=Blockchain hash corpus addressing and reconstruction research tool
Exec=$BIN/block-n-pick-gui
Icon=applications-development
Terminal=false
Categories=Development;Utility;
EOF
printf 'Installed Block-n-Pick launcher: %s
' "$APPS/block-n-pick.desktop"
