#!/usr/bin/env bash
# Abre el reproductor de capturadora en su propia ventana (GTK4 + GStreamer del runtime de GNOME).
DIR="$HOME/.local/share/capturadora"
mkdir -p "$HOME/.config/capturadora"

limpiar() {   # quita la conexión de audio si la app se cerró de golpe
  pactl list short modules 2>/dev/null | awk -F'\t' '$2=="module-loopback" && $3 ~ /application.name=Capturadora/ {print $1}' \
    | xargs -r -n1 pactl unload-module 2>/dev/null
}

for v in 50 49 48 47 46; do
  if flatpak info "org.gnome.Platform//$v" >/dev/null 2>&1; then
    flatpak run --command=python3 \
      --socket=wayland --socket=fallback-x11 --share=ipc --device=all --socket=pulseaudio \
      --filesystem=xdg-run/pipewire-0 --talk-name=org.freedesktop.Flatpak \
      --filesystem="$DIR":ro --filesystem="$HOME/.config/capturadora" \
      "org.gnome.Platform//$v" "$DIR/capturadora-app.py"
    limpiar
    exit 0
  fi
done

notify-send "Capturadora" "Falta el componente de GNOME. Vuelve a correr install.sh" 2>/dev/null
