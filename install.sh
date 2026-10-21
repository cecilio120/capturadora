#!/usr/bin/env bash
# Instala la Capturadora para tu usuario.
set -e
cd "$(dirname "$0")"
HAVE_RT=""
for v in 50 49 48 47 46; do
  flatpak info "org.gnome.Platform//$v" >/dev/null 2>&1 && HAVE_RT=1 && break
done
if [ -z "$HAVE_RT" ]; then
  flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  flatpak install --user -y flathub org.gnome.Platform//48
fi
DEST="$HOME/.local/share/capturadora"
mkdir -p "$DEST" "$HOME/.local/share/applications"
cp capturadora-app.py abrir.sh "$DEST/"
chmod +x "$DEST/abrir.sh"
cat > "$HOME/.local/share/applications/io.github.capturadora.Capturadora.desktop" <<D
[Desktop Entry]
Type=Application
Name=Capturadora
Comment=Ver y escuchar tu capturadora en pantalla
Exec=$DEST/abrir.sh
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;Player;
StartupWMClass=io.github.capturadora.Capturadora
D
echo "Listo. Abre «Capturadora» desde el menú."
