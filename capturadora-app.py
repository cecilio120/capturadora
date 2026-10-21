#!/usr/bin/env python3
"""
Capturadora — visor nativo (GTK4 + GStreamer) para tarjetas de captura.

Lee la capturadora directamente con GStreamer, prefiere el formato MJPEG
(el único con el que casi todas las capturadoras USB dan 1080p a 60 fps)
y reproduce su audio con baja latencia.

Teclas:  F / F11 pantalla completa · Esc salir de pantalla completa · M silenciar
"""
import json
import os
import re
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, GstApp, Gtk  # noqa: E402,F401

Gst.init(None)

APP_ID = "io.github.capturadora.Capturadora"
CONF_DIR = os.path.expanduser("~/.config/capturadora")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
TEST = os.environ.get("CAPTURADORA_TEST") == "1"
CAPTURE_RE = re.compile(r"captur|elgato|cam ?link|hd60|hdmi|usb video|avermedia|live gamer|ugreen|"
                        r"mirabox|macrosilicon|ms2109|ms2130|genki|ripsaw|evga|magewell|av to usb|grabber",
                        re.I)

CSS = b"""
window, .stage { background: #0d0c0b; }
* { font-family: "Nunito", "Quicksand", "Noto Sans", sans-serif; }
.bar { background: linear-gradient(to bottom, rgba(13,12,11,.92), rgba(13,12,11,0)); padding: 12px 16px 30px; }
.brand { font-weight: 900; font-size: 18px; color: #f1ede4; margin-right: 8px; }
.dot { min-width: 10px; min-height: 10px; border-radius: 99px; background: #625d54; }
.dot.live { background: #c6f432; box-shadow: 0 0 8px rgba(198,244,50,.7); }
.dot.err { background: #ff5a4a; }
.bar button, .bar dropdown > button, .pill {
  background: rgba(29,28,25,.92); color: #f1ede4; border: 1px solid #34312b;
  border-radius: 12px; min-height: 34px; box-shadow: none; font-weight: 700; }
.bar button:hover, .bar dropdown > button:hover { background: #262420; border-color: #4b473f; }
.bar button.on { background: #ff5a4a; color: #1a0d0b; border-color: #ff5a4a; }
.pill { padding: 0 10px 0 4px; }
.pill button { border: 0; background: transparent; }
.pill scale { min-width: 110px; }
.pill scale trough { background: #0d0c0b; min-height: 8px; border-radius: 99px; }
.pill scale highlight { background: #c6f432; border-radius: 99px; }
.pill scale slider { background: #141311; border: 3px solid #c6f432; min-width: 14px; min-height: 14px;
  border-radius: 99px; margin: -6px; }
.vol-label { color: #9b9488; font-size: 12px; font-weight: 800; min-width: 40px; }
.badge { background: rgba(29,28,25,.92); color: #f1ede4; border: 1px solid #34312b;
  border-radius: 99px; padding: 4px 12px; font-size: 12px; font-weight: 800; margin: 14px; }
.badge.low { color: #ffb547; border-color: #ffb547; }
.empty-title { color: #f1ede4; font-size: 19px; font-weight: 900; }
.empty-sub { color: #9b9488; }
popover contents { background: #1d1c19; color: #f1ede4; border-radius: 12px; }
popover row:selected { background: #c6f432; color: #141311; }
"""


# Nitidez adaptativa (estilo CAS) para la imagen escalada por la GPU
SHARPEN = """#version 100
#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float width;
uniform float height;
void main () {
  vec2 px = vec2(1.0/width, 1.0/height);
  vec3 c = texture2D(tex, v_texcoord).rgb;
  vec3 n = texture2D(tex, v_texcoord + vec2(0.0, -px.y)).rgb;
  vec3 s = texture2D(tex, v_texcoord + vec2(0.0,  px.y)).rgb;
  vec3 e = texture2D(tex, v_texcoord + vec2( px.x, 0.0)).rgb;
  vec3 w = texture2D(tex, v_texcoord + vec2(-px.x, 0.0)).rgb;
  vec3 mn = min(c, min(min(n, s), min(e, w)));
  vec3 mx = max(c, max(max(n, s), max(e, w)));
  vec3 amp = sqrt(clamp(min(mn, 1.0 - mx) / max(mx, 0.0001), 0.0, 1.0));
  vec3 wgt = -amp / 6.0;
  vec3 r = (c + (n + s + e + w) * wgt) / (1.0 + 4.0 * wgt);
  gl_FragColor = vec4(clamp(r, 0.0, 1.0), 1.0);
}
"""


# ─────────────────────────── utilidades ───────────────────────────
def load_cfg():
    base = {"video": "", "mode": "", "audio": "", "volume": 100, "muted": False, "fill": False,
            "audio_latency": 15, "sharpen": True}
    try:
        with open(CONF_FILE) as f:
            base.update(json.load(f))
    except Exception:
        pass
    return base


def save_cfg(cfg):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONF_FILE)
    except Exception as e:
        print("no pude guardar la configuración:", e)


def parse_modes(caps):
    """Lista de modos (formato, ancho, alto, fps) a partir de las caps del dispositivo."""
    modes = set()
    if caps is None:
        return []
    for i in range(caps.get_size()):
        s = caps.get_structure(i)
        name = s.get_name()
        if name not in ("image/jpeg", "video/x-raw"):
            continue
        txt = s.to_string()
        fmt = "MJPEG" if name == "image/jpeg" else (re.search(r"format=\(string\)(\w+)", txt) or [None, "RAW"])[1]
        w = re.search(r"width=\(int\)(\d+)", txt)
        h = re.search(r"height=\(int\)(\d+)", txt)
        if not (w and h):
            continue
        fr = re.search(r"framerate=\(fraction\)(\{[^}]*\}|\[[^\]]*\]|[\d/]+)", txt)
        rates = []
        if fr:
            for n, d in re.findall(r"(\d+)/(\d+)", fr.group(1)):
                if int(d):
                    rates.append(int(n) / int(d))
        fps = max(rates) if rates else 30
        if fr and fr.group(1).startswith("{"):
            for r in rates:
                modes.add((fmt, int(w.group(1)), int(h.group(1)), round(r)))
        else:
            modes.add((fmt, int(w.group(1)), int(h.group(1)), round(fps)))
    def score(m):
        fmt, w, h, fps = m
        return (h, fps, fmt == "MJPEG")
    return sorted(modes, key=score, reverse=True)


def short(name, n=24):
    return name if len(name) <= n else name[:n - 1].rstrip() + "…"


def mode_key(m):
    return f"{m[0]}:{m[1]}x{m[2]}@{m[3]}"


def mode_label(m):
    fmt, w, h, fps = m
    nice = {2160: "4K", 1440: "1440p", 1080: "1080p", 720: "720p", 480: "480p"}.get(h, f"{w}×{h}")
    return f"{nice} · {fps} fps · {fmt}"


def best_mode(modes):
    """Prefiere 1080p con más fps en MJPEG; si no, lo más grande y fluido."""
    def pref(m):
        fmt, w, h, fps = m
        return (h <= 1080 and fps >= 50, h == 1080, fmt == "MJPEG", fps, h)
    return max(modes, key=pref) if modes else None


# ─────────────────────────── dispositivos ───────────────────────────
class Dev:
    def __init__(self, name, element_factory, modes=None, path=None):
        self.name = name
        self.make = element_factory
        self.modes = modes or []
        self.path = path
        self.api = "v4l2"


# ─────────────── audio por PipeWire (directo, sin pasar por la app) ───────────────
import subprocess  # noqa: E402

IN_FLATPAK = os.path.exists("/.flatpak-info")
LOOP_NAME = "Capturadora"


def host_pactl(*args, js=False):
    cmd = (["flatpak-spawn", "--host"] if IN_FLATPAK else []) + ["pactl"] + \
          (["-f", "json"] if js else []) + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    if js:
        try:
            return json.loads(r.stdout or "[]")
        except ValueError:
            return None
    return r.stdout


def _good(v):
    return isinstance(v, str) and v.strip() not in ("", "(null)")


def pa_sources():
    """Fuentes de audio del sistema (None si no se puede usar pactl)."""
    data = host_pactl("list", "sources", js=True)
    if data is None:
        return None
    out = []
    for d in data:
        props = d.get("properties") or {}
        name = next((v for v in (d.get("name"), props.get("node.name")) if _good(v)), "")
        if not name or name.endswith(".monitor") or name.startswith("mezclador_"):
            continue
        if props.get("device.class") == "monitor":
            continue
        desc = next((v for v in (d.get("description"), props.get("device.description"),
                                  props.get("node.description"), name) if _good(v)), name)
        dev = Dev(desc, None)
        dev.pa_name = name
        out.append(dev)
    return out


def pa_cleanup():
    out = host_pactl("list", "short", "modules") or ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] == "module-loopback" and f"application.name={LOOP_NAME}" in parts[2]:
            host_pactl("unload-module", parts[0])


def pa_loop_index():
    for i in host_pactl("list", "sink-inputs", js=True) or []:
        if (i.get("properties") or {}).get("application.name") == LOOP_NAME:
            return i.get("index")
    return None


_MONITOR = None


def get_monitor():
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = Gst.DeviceMonitor.new()
        _MONITOR.add_filter("Video/Source", None)
        _MONITOR.add_filter("Audio/Source", None)
        _MONITOR.start()
    return _MONITOR


def gst_devices():
    vids, auds = [], []
    if TEST:
        modes = [("MJPEG", 1920, 1080, 60), ("MJPEG", 1920, 1080, 30), ("YUY2", 1920, 1080, 5),
                 ("MJPEG", 1280, 720, 60)]
        vids.append(Dev("USB3.0 Capture (prueba)", lambda: Gst.ElementFactory.make("videotestsrc"), modes))
        auds.append(Dev("USB3.0 Capture Estéreo analógico (prueba)",
                        lambda: Gst.ElementFactory.make("audiotestsrc")))
        return vids, auds
    mon = get_monitor()
    for d in mon.get_devices():
        props = d.get_properties()
        api = props.get_string("device.api") if props else None
        cls = d.get_device_class()
        name = d.get_display_name()
        if "Video/Source" in cls:
            if api not in (None, "v4l2", "pipewire"):
                continue
            path = (props.get_string("device.path") or props.get_string("api.v4l2.path")) if props else None
            modes = parse_modes(d.get_caps())
            if not modes:
                continue
            if path and path.startswith("/dev/video") and Gst.ElementFactory.find("v4l2src"):
                # Leer la capturadora directo por V4L2: por PipeWire llegan muchos menos cuadros
                def make_v4l2(pp=path):
                    e = Gst.ElementFactory.make("v4l2src")
                    e.set_property("device", pp)
                    return e
                dev = Dev(name, make_v4l2, modes, path)
            else:
                dev = Dev(name, (lambda dd=d: dd.create_element(None)), modes, path)
            dev.api = api or "v4l2"
            vids.append(dev)
        elif "Audio/Source" in cls:
            if props and props.get_string("device.class") == "monitor":
                continue
            if "monitor" in name.lower() and "of" in name.lower():
                continue
            auds.append(Dev(name, (lambda dd=d: dd.create_element(None))))
    pa = pa_sources()
    if pa is not None:
        auds = pa
    # Si el mismo dispositivo aparece por v4l2 y por pipewire, quedarse con v4l2 (más directo)
    vids.sort(key=lambda v: v.api != "v4l2")
    out, seen = [], set()
    for v in vids:
        if v.name not in seen:
            seen.add(v.name)
            out.append(v)
    return out, auds


# ─────────────────────────── ventana ───────────────────────────
class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Capturadora")
        self.set_default_size(1360, 780)
        self.set_icon_name("camera-video")
        self.cfg = load_cfg()
        self.vids, self.auds = [], []
        self.vpipe = self.apipe = None
        self.updating = False
        self.frame_lock = threading.Lock()
        self.pending = None
        self.idle_scheduled = False
        self.frames = 0
        self.last_fps_t = time.monotonic()
        self.cur_size = (0, 0)
        self.fmt = (Gdk.MemoryFormat.B8G8R8X8 if hasattr(Gdk.MemoryFormat, "B8G8R8X8")
                    else Gdk.MemoryFormat.B8G8R8A8)
        self.hide_id = None

        prov = Gtk.CssProvider()
        if hasattr(prov, "load_from_string"):
            prov.load_from_string(CSS.decode())
        else:
            prov.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        overlay = Gtk.Overlay()
        overlay.add_css_class("stage")
        self.set_child(overlay)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.apply_fill()
        overlay.set_child(self.picture)

        # Mensaje cuando no hay señal
        self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                             halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        icon.set_pixel_size(56)
        icon.set_opacity(.4)
        self.empty_t = Gtk.Label(label="Buscando tu capturadora…", css_classes=["empty-title"])
        self.empty_s = Gtk.Label(label="Conéctala por USB y elígela arriba.", css_classes=["empty-sub"])
        for w in (icon, self.empty_t, self.empty_s):
            self.empty.append(w)
        overlay.add_overlay(self.empty)

        # Barra superior
        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                     reveal_child=True, valign=Gtk.Align.START)
        bar = Gtk.Box(spacing=8, css_classes=["bar"])
        self.revealer.set_child(bar)
        overlay.add_overlay(self.revealer)

        brand = Gtk.Box(spacing=8)
        self.dot = Gtk.Box(css_classes=["dot"], valign=Gtk.Align.CENTER)
        brand.append(self.dot)
        brand_l = Gtk.Label(css_classes=["brand"])
        brand_l.set_markup('Captura<span color="#c6f432">.</span>')
        brand.append(brand_l)
        bar.append(brand)

        self.vlist, self.mlist, self.alist = Gtk.StringList(), Gtk.StringList(), Gtk.StringList()
        self.vdrop = Gtk.DropDown(model=self.vlist, tooltip_text="Capturadora (video)")
        self.mdrop = Gtk.DropDown(model=self.mlist, tooltip_text="Calidad")
        self.adrop = Gtk.DropDown(model=self.alist, tooltip_text="Audio")
        for d in (self.vdrop, self.mdrop, self.adrop):
            d.set_size_request(120, -1)
            bar.append(d)
        self.vdrop.connect("notify::selected", self.on_video_changed)
        self.mdrop.connect("notify::selected", self.on_mode_changed)
        self.adrop.connect("notify::selected", self.on_audio_changed)

        # Retraso del audio: menos = más inmediato (si se oye cortado, subirlo)
        self.LAT = [8, 15, 25, 40]
        self.ldrop = Gtk.DropDown.new_from_strings(["Audio 8 ms", "Audio 15 ms", "Audio 25 ms", "Audio 40 ms"])
        self.ldrop.set_tooltip_text("Retraso del audio. Si se oye cortado, sube un nivel.")
        lat = self.cfg.get("audio_latency", 15)
        self.ldrop.set_selected(self.LAT.index(lat) if lat in self.LAT else 1)
        self.ldrop.connect("notify::selected", self.on_latency_changed)
        bar.append(self.ldrop)

        pill = Gtk.Box(spacing=4, css_classes=["pill"], valign=Gtk.Align.CENTER)
        self.mute_btn = Gtk.Button(icon_name="audio-volume-high-symbolic", tooltip_text="Silenciar (M)")
        self.mute_btn.connect("clicked", lambda *_: self.toggle_mute())
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_value(self.cfg["volume"])
        self.scale.connect("value-changed", self.on_volume)
        self.vol_label = Gtk.Label(css_classes=["vol-label"])
        for w in (self.mute_btn, self.scale, self.vol_label):
            pill.append(w)
        bar.append(pill)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        self.fill_btn = Gtk.Button(label="Ajustar")
        self.fill_btn.connect("clicked", lambda *_: self.toggle_fill())
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reconectar")
        reload_btn.connect("clicked", lambda *_: self.refresh_devices(restart=True))
        fs_btn = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Pantalla completa (F)")
        fs_btn.connect("clicked", lambda *_: self.toggle_fs())
        self.sharp_btn = Gtk.Button()
        self.sharp_btn.connect("clicked", lambda *_: self.toggle_sharpen())
        self.update_sharpen_btn()
        for w in (self.sharp_btn, self.fill_btn, reload_btn, fs_btn):
            bar.append(w)

        # Indicador de resolución / fps
        self.badge = Gtk.Label(css_classes=["badge"], halign=Gtk.Align.END, valign=Gtk.Align.END)
        overlay.add_overlay(self.badge)

        # Entradas: mouse, doble clic, teclado
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda *_: self.wake())
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_click)
        self.picture.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.update_volume_ui()
        self.update_fill_btn()
        self.connect("close-request", self.on_close)
        GLib.timeout_add(1000, self.tick_fps)
        self.over_bar = False
        barmotion = Gtk.EventControllerMotion()
        barmotion.connect("enter", lambda *_: setattr(self, "over_bar", True))
        barmotion.connect("leave", lambda *_: setattr(self, "over_bar", False))
        bar.add_controller(barmotion)
        if not TEST:
            mbus = get_monitor().get_bus()
            mbus.add_watch(GLib.PRIORITY_DEFAULT, self.on_monitor_msg)
        self.poll_id = None
        self.refresh_devices(restart=True)
        self.wake()

    # ── estado ──
    def set_status(self, kind, title=None, sub=None):
        for c in ("live", "err"):
            self.dot.remove_css_class(c)
        if kind in ("live", "err"):
            self.dot.add_css_class(kind)
        self.empty.set_visible(kind != "live")
        if title:
            self.empty_t.set_label(title)
        if sub is not None:
            self.empty_s.set_label(sub)

    # ── dispositivos ──
    def on_monitor_msg(self, bus, msg):
        if msg.type in (Gst.MessageType.DEVICE_ADDED, Gst.MessageType.DEVICE_REMOVED):
            if self.poll_id:
                GLib.source_remove(self.poll_id)
            self.poll_id = GLib.timeout_add(700, self.poll_devices)
        return True

    def poll_devices(self):
        """Detecta si conectaste o desconectaste la capturadora."""
        self.poll_id = None
        vids, auds = gst_devices()
        if [d.name for d in vids] != [d.name for d in self.vids] or \
           [d.name for d in auds] != [d.name for d in self.auds]:
            lost = self.vpipe is None or self.current_video() is None or \
                   self.current_video().name not in [d.name for d in vids]
            self.refresh_devices(restart=lost, vids=vids, auds=auds)
        return False

    def refresh_devices(self, restart=False, vids=None, auds=None):
        if vids is None:
            vids, auds = gst_devices()
        self.vids, self.auds = vids, auds
        self.updating = True
        self.vlist.splice(0, self.vlist.get_n_items(), [short(d.name) for d in vids] or ["Sin capturadora"])
        self.alist.splice(0, self.alist.get_n_items(), ["Sin audio"] + [short(d.name) for d in auds])
        names = [d.name for d in vids]
        if self.cfg["video"] in names:
            vi = names.index(self.cfg["video"])
        else:
            vi = next((i for i, d in enumerate(vids) if CAPTURE_RE.search(d.name)), 0)
        self.vdrop.set_selected(vi)
        anames = [d.name for d in auds]
        if self.cfg["audio"] == "__none__":
            ai = 0
        elif self.cfg["audio"] in anames:
            ai = anames.index(self.cfg["audio"]) + 1
        else:
            ai = next((i + 1 for i, d in enumerate(auds) if CAPTURE_RE.search(d.name)), 0)
        self.adrop.set_selected(ai)
        self.fill_modes()
        self.updating = False
        if restart:
            self.start_video()
            self.start_audio()

    def current_video(self):
        i = self.vdrop.get_selected()
        return self.vids[i] if 0 <= i < len(self.vids) else None

    def current_audio(self):
        i = self.adrop.get_selected()
        return self.auds[i - 1] if 1 <= i <= len(self.auds) else None

    def fill_modes(self):
        dev = self.current_video()
        modes = dev.modes if dev else []
        self.mlist.splice(0, self.mlist.get_n_items(), [mode_label(m) for m in modes] or ["—"])
        keys = [mode_key(m) for m in modes]
        if self.cfg["mode"] in keys:
            self.mdrop.set_selected(keys.index(self.cfg["mode"]))
        elif modes:
            self.mdrop.set_selected(modes.index(best_mode(modes)))

    def current_mode(self):
        dev = self.current_video()
        i = self.mdrop.get_selected()
        return dev.modes[i] if dev and 0 <= i < len(dev.modes) else None

    def on_video_changed(self, *_):
        if self.updating:
            return
        dev = self.current_video()
        self.cfg["video"] = dev.name if dev else ""
        self.cfg["mode"] = ""
        save_cfg(self.cfg)
        self.updating = True
        self.fill_modes()
        self.updating = False
        self.start_video()

    def on_mode_changed(self, *_):
        if self.updating:
            return
        m = self.current_mode()
        self.cfg["mode"] = mode_key(m) if m else ""
        save_cfg(self.cfg)
        self.start_video()

    def on_latency_changed(self, *_):
        self.cfg["audio_latency"] = self.LAT[self.ldrop.get_selected()]
        save_cfg(self.cfg)
        self.start_audio()

    def on_audio_changed(self, *_):
        if self.updating:
            return
        dev = self.current_audio()
        self.cfg["audio"] = dev.name if dev else "__none__"
        save_cfg(self.cfg)
        self.start_audio()

    # ── video ──
    def stop_pipe(self, pipe):
        if pipe:
            pipe.set_state(Gst.State.NULL)

    def start_video(self):
        self.stop_pipe(self.vpipe)
        self.vpipe = None
        dev, mode = self.current_video(), self.current_mode()
        if not dev:
            self.picture.set_paintable(None)
            self.set_status("err", "No encuentro ninguna capturadora", "Conéctala por USB; la detecto sola.")
            return
        fmt, w, h, fps = mode if mode else ("MJPEG", 1920, 1080, 60)
        pipe = Gst.Pipeline.new("video")
        src = dev.make()
        if src is None:
            self.set_status("err", "No pude abrir la capturadora", dev.name)
            return
        if TEST:
            src.set_property("is-live", True)
            src.set_property("pattern", "smpte")
        caps_str = (f"image/jpeg,width={w},height={h},framerate={fps}/1" if fmt == "MJPEG" and not TEST
                    else f"video/x-raw,width={w},height={h},framerate={fps}/1"
                         + ("" if TEST else f",format={fmt}"))
        capsf = Gst.ElementFactory.make("capsfilter")
        capsf.set_property("caps", Gst.Caps.from_string(caps_str))
        q = Gst.ElementFactory.make("queue")
        q.set_property("leaky", 2)
        q.set_property("max-size-buffers", 1)
        chain = [src, capsf, q]
        if fmt == "MJPEG" and not TEST:
            dec = Gst.ElementFactory.make("jpegdec")
            chain.append(dec)
        conv = Gst.ElementFactory.make("videoconvert")
        try:
            conv.set_property("n-threads", 4)
        except TypeError:
            pass
        gtksink = None if os.environ.get("CAPTURADORA_APPSINK") else Gst.ElementFactory.make("gtk4paintablesink")
        gpu = self.gpu_chain(h) if gtksink is not None else None
        if gtksink is not None:
            # Camino directo a la GPU: menos copias y menos retraso
            gtksink.set_property("sync", False)
            self.picture.set_paintable(gtksink.get_property("paintable"))
            sink = gtksink
            sinkpad = sink.get_static_pad("sink")
            sinkpad.add_probe(Gst.PadProbeType.BUFFER, self.on_probe_frame)
            chain += (gpu or [conv]) + [sink]
        else:
            outcaps = Gst.ElementFactory.make("capsfilter")
            outcaps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRx"))
            sink = Gst.ElementFactory.make("appsink")
            sink.set_property("emit-signals", True)
            sink.set_property("max-buffers", 1)
            sink.set_property("drop", True)
            sink.set_property("sync", False)
            sink.connect("new-sample", self.on_sample)
            chain += [conv, outcaps, sink]
        for e in chain:
            pipe.add(e)
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                self.set_status("err", "La capturadora no acepta ese modo", "Prueba otra calidad arriba.")
                return
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_video_error)
        self.vpipe = pipe
        self.cur_size = (w, h)
        self.expected_fps = fps
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.set_status("err", "No pude iniciar el video", "¿Otra app (OBS) está usando la capturadora?")
            return
        self.set_status("wait", "Conectando…", dev.name)

    def gpu_chain(self, h):
        """Escala a 1080p en la GPU y aplica nitidez (solo si la fuente es menor de 1080p)."""
        if not self.cfg.get("sharpen", True) or h >= 1080:
            return None
        els = [Gst.ElementFactory.make(n) for n in ("glupload", "glcolorconvert", "glcolorscale",
                                                   "capsfilter", "glshader")]
        if None in els:
            return None
        els[3].set_property("caps", Gst.Caps.from_string(
            "video/x-raw(memory:GLMemory),format=RGBA,width=1920,height=1080"))
        els[4].set_property("fragment", SHARPEN)
        dl = Gst.ElementFactory.make("gldownload")
        if dl is not None:
            # Devolver los cuadros a memoria normal: GTK los sube sin esperar a la GPU (60 fps en vez de 30)
            els += [dl, Gst.ElementFactory.make("videoconvert")]
        return els

    def toggle_sharpen(self):
        self.cfg["sharpen"] = not self.cfg.get("sharpen", True)
        self.update_sharpen_btn()
        save_cfg(self.cfg)
        self.start_video()

    def update_sharpen_btn(self):
        on = self.cfg.get("sharpen", True)
        self.sharp_btn.set_label("Nítido" if on else "Normal")
        self.sharp_btn.set_tooltip_text("Escalado por GPU con nitidez (para 720p)" if on
                                        else "Escalado normal")

    def on_video_error(self, bus, msg):
        err, dbg = msg.parse_error()
        print("error de video:", err.message, dbg)
        busy = "busy" in (err.message + str(dbg)).lower()
        self.set_status("err", "No pude abrir el video",
                        "La está usando otra app (¿OBS?). Ciérrala y pulsa Reconectar." if busy
                        else err.message)
        self.stop_pipe(self.vpipe)
        self.vpipe = None

    def on_probe_frame(self, pad, info):
        self.frames += 1
        if self.frames == 1:
            GLib.idle_add(lambda: (self.empty.get_visible() and self.set_status("live"), False)[1])
        return Gst.PadProbeReturn.OK

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = GLib.Bytes.new(info.data)
        buf.unmap(info)
        with self.frame_lock:
            self.pending = (data, w, h)
            if not self.idle_scheduled:
                self.idle_scheduled = True
                GLib.idle_add(self.show_frame, priority=GLib.PRIORITY_HIGH)
        return Gst.FlowReturn.OK

    def show_frame(self):
        with self.frame_lock:
            frame, self.pending = self.pending, None
            self.idle_scheduled = False
        if frame:
            data, w, h = frame
            tex = Gdk.MemoryTexture.new(w, h, self.fmt, data, w * 4)
            self.picture.set_paintable(tex)
            self.frames += 1
            if self.empty.get_visible():
                self.set_status("live")
            self.cur_size = (w, h)
        return False

    def tick_fps(self):
        now = time.monotonic()
        fps = self.frames / (now - self.last_fps_t)
        self.frames, self.last_fps_t = 0, now
        if os.environ.get("CAPTURADORA_FPSLOG"):
            print(f"fps {fps:.1f}", flush=True)
        if self.vpipe and fps > 0:
            w, h = self.cur_size
            self.badge.set_label(f"{w}×{h} · {round(fps)} fps")
            if fps < 0.85 * getattr(self, "expected_fps", fps):
                self.badge.add_css_class("low")
            else:
                self.badge.remove_css_class("low")
            self.badge.set_visible(True)
        else:
            self.badge.set_visible(False)
        return True

    # ── audio ──
    def start_audio(self):
        self.stop_pipe(self.apipe)
        self.apipe = None
        self.loop_mod = None
        if not TEST:
            pa_cleanup()
        dev = self.current_audio()
        if not dev:
            return
        if getattr(dev, "pa_name", None):
            out = host_pactl("load-module", "module-loopback", f"source={dev.pa_name}",
                             f"latency_msec={int(self.cfg.get('audio_latency', 15))}", "source_dont_move=true",
                             f"sink_input_properties=application.name={LOOP_NAME}",
                             f"source_output_properties=application.name={LOOP_NAME}")
            self.loop_mod = (out or "").strip() or None
            if not self.loop_mod:
                print("no pude crear la conexión de audio")
            GLib.timeout_add(400, lambda: (self.apply_volume(), False)[1])
            return
        pipe = Gst.Pipeline.new("audio")
        src = dev.make()
        if src is None:
            return
        if TEST:
            src.set_property("is-live", True)
            src.set_property("volume", 0.05)
        else:
            # Búfer más amplio: las capturadoras USB mandan el audio "a rachas";
            # con búferes muy chicos se oye cortado.
            for prop, val in (("buffer-time", 100000), ("latency-time", 20000), ("do-timestamp", True)):
                try:
                    src.set_property(prop, val)
                except TypeError:
                    pass
        q = Gst.ElementFactory.make("queue")
        q.set_property("max-size-time", 300_000_000)
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-bytes", 0)
        conv = Gst.ElementFactory.make("audioconvert")
        res = Gst.ElementFactory.make("audioresample")
        self.vol = Gst.ElementFactory.make("volume")
        sink = Gst.ElementFactory.make("fakesink" if TEST else "pulsesink")
        if not TEST:
            sink.set_property("buffer-time", 100000)
            sink.set_property("latency-time", 20000)
            sink.set_property("sync", True)
            try:
                sink.set_property("slave-method", "resample")   # corrige la deriva de reloj sin cortes
            except TypeError:
                pass
            props = Gst.Structure.new_from_string("props,media.role=game,application.name=Capturadora")
            try:
                sink.set_property("stream-properties", props)
            except TypeError:
                pass
        chain = [src, q, conv, res, self.vol, sink]
        for e in chain:
            pipe.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda b, m: print("error de audio:", m.parse_error()[0].message))
        self.apipe = pipe
        self.apply_volume()
        pipe.set_state(Gst.State.PLAYING)

    def apply_volume(self):
        if getattr(self, "loop_mod", None):
            idx = pa_loop_index()
            if idx is not None:
                host_pactl("set-sink-input-volume", idx, f"{int(self.cfg['volume'])}%")
                host_pactl("set-sink-input-mute", idx, "1" if self.cfg["muted"] else "0")
            return
        if self.apipe:
            self.vol.set_property("volume", self.cfg["volume"] / 100)
            self.vol.set_property("mute", self.cfg["muted"])

    def update_volume_ui(self):
        self.vol_label.set_label("Mudo" if self.cfg["muted"] else f"{int(self.cfg['volume'])}%")
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if self.cfg["muted"]
                                    else "audio-volume-high-symbolic")
        (self.mute_btn.add_css_class if self.cfg["muted"] else self.mute_btn.remove_css_class)("on")

    def on_volume(self, scale):
        self.cfg["volume"] = int(scale.get_value())
        self.cfg["muted"] = False
        self.apply_volume()
        self.update_volume_ui()
        save_cfg(self.cfg)

    def toggle_mute(self):
        self.cfg["muted"] = not self.cfg["muted"]
        self.apply_volume()
        self.update_volume_ui()
        save_cfg(self.cfg)

    # ── vista ──
    def apply_fill(self):
        self.picture.set_content_fit(Gtk.ContentFit.FILL if self.cfg["fill"] else Gtk.ContentFit.CONTAIN)

    def update_fill_btn(self):
        self.fill_btn.set_label("Estirar" if self.cfg["fill"] else "Ajustar")
        self.fill_btn.set_tooltip_text("La imagen llena toda la ventana" if self.cfg["fill"]
                                       else "Mantiene la proporción original")

    def toggle_fill(self):
        self.cfg["fill"] = not self.cfg["fill"]
        self.apply_fill()
        self.update_fill_btn()
        save_cfg(self.cfg)

    def toggle_fs(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_click(self, gesture, n_press, x, y):
        if n_press == 2:
            self.toggle_fs()

    def on_key(self, ctrl, keyval, keycode, state):
        name = Gdk.keyval_name(keyval) or ""
        if name in ("f", "F", "F11"):
            self.toggle_fs()
            return True
        if name == "Escape" and self.is_fullscreen():
            self.unfullscreen()
            return True
        if name in ("m", "M"):
            self.toggle_mute()
            return True
        return False

    def wake(self):
        self.revealer.set_reveal_child(True)
        self.badge.set_opacity(1)
        self.set_cursor(None)
        if self.hide_id:
            GLib.source_remove(self.hide_id)
        self.hide_id = GLib.timeout_add(2500, self.go_idle)

    def go_idle(self):
        self.hide_id = None
        if self.vpipe and not self.over_bar:
            self.revealer.set_reveal_child(False)
            self.badge.set_opacity(0)
            self.set_cursor(Gdk.Cursor.new_from_name("none", None))
        return False

    def on_close(self, *_):
        self.stop_pipe(self.vpipe)
        self.stop_pipe(self.apipe)
        if not TEST:
            pa_cleanup()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        gs = Gtk.Settings.get_default()
        if gs:
            gs.set_property("gtk-application-prefer-dark-theme", True)
        win = Window(self)
        win.present()
        if TEST and os.environ.get("CAPTURADORA_TEST_QUIT"):
            GLib.timeout_add_seconds(int(os.environ["CAPTURADORA_TEST_QUIT"]), self.quit)


if __name__ == "__main__":
    App().run(None)
