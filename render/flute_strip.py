"""Per-frame flute view: a real CC0 alto flute with glowing key pads and breath.

The instrument is a CC0 photograph of a Boehm & Mendler alto flute (Met object
505641), background removed and placed diagonally on the stage. The photo's key
cups are used as the playing surface: every note lights the pad under the
fingering position and sends a soft breath plume from the embouchure. The
ornaments in the composition (grace notes and trills) read as quick pad flashes.
"""
import math
import queue
import random
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from config import FPS, H, W
from render.scene import load_font, make_spark_sprite, palette
from render.score_layer import PANEL_H, PANEL_W, PANEL_X, PANEL_Y

# ---------------------------------------------------------------------------
# Photo / crop geometry, in the 2500 x 4000 source image.
# ---------------------------------------------------------------------------
PHOTO_W, PHOTO_H = 2500, 4000
CROP_X0, CROP_Y0, CROP_X1, CROP_Y1 = 1000, 40, 1550, 3740
CROP_W, CROP_H = CROP_X1 - CROP_X0, CROP_Y1 - CROP_Y0

# Instrument centre in crop coordinates, and the screen affine.
INSTR_CX, INSTR_CY = 265.0, 1835.0
SCALE = 0.30
ANGLE = 28.0
CENTER_X, CENTER_Y = 655.0, 546.0

BODY_PHOTO = Path(__file__).resolve().parents[1] / "assets" / "flute_body.jpg"

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# The glowing "fingering" position travels along the tube. High notes sit near
# the embouchure/headjoint, low notes near the footjoint; the exact key cup
# under the glow is less important than the light travelling with the phrase.
N_KEYS = 15
KEY_X = 278.0
KEY_Y = tuple(float(y) for y in np.linspace(420.0, 3420.0, N_KEYS))
EMB_X, EMB_Y = 265.0, 330.0


def _glow_sprite(size):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    r = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / c
    return np.clip(1.0 - r, 0, 1) ** 2.4


def _glow_rgba(size, color):
    alpha = _glow_sprite(size)
    rgb = np.ones((size, size, 3), np.float32) * color[None, None, :]
    return np.dstack([rgb, alpha * 255.0]).astype(np.uint8)


def _ring_rgba(size, radius, width, color):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    r = np.sqrt((xx - c) ** 2 + (yy - c) ** 2)
    alpha = np.exp(-((r - radius) / width) ** 2)
    rgb = np.ones((size, size, 3), np.float32) * color[None, None, :]
    return np.dstack([rgb, np.clip(alpha, 0, 1) * 255.0]).astype(np.uint8)


def _capsule_rgba(w, h, sx, sy, color):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    alpha = np.exp(-((xx - cx) / sx) ** 2 - ((yy - cy) / sy) ** 2)
    rgb = np.ones((h, w, 3), np.float32) * color[None, None, :]
    return np.dstack([rgb, np.clip(alpha, 0, 1) * 255.0]).astype(np.uint8)


def _clip_add(roi, add):
    roi[:] = np.minimum(roi.astype(np.uint16) + add.astype(np.uint16),
                        255).astype(np.uint8)


def _photo_mask(rgb):
    """Alpha for the flute: the studio backdrop is a smooth grey gradient."""
    h, w = rgb.shape[:2]
    # The flute is centred; estimate the backdrop from both side margins.
    sides = np.concatenate([rgb[:, :30], rgb[:, -30:]], axis=1).astype(np.float32)
    bg = np.median(sides, axis=1)
    diff = np.max(np.abs(rgb.astype(np.float32) - bg[:, None, :]), axis=2)
    mx = rgb.max(axis=2).astype(np.int16)
    mn = rgb.min(axis=2).astype(np.int16)
    fg = ((diff > 10) | ((mx - mn) > 38)).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((25, 3), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n > 1:
        fg = (lab == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
    inv = (1 - fg).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(inv, 8)
    for k in range(1, n):
        x, y, bw, bh, area = stats[k]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
            continue
        fg[lab == k] = 1
    return cv2.GaussianBlur(fg.astype(np.float32) * 255, (0, 0), 1.6)


_BODY_CACHE = {}


def _body_photo():
    cached = _BODY_CACHE.get("body")
    if cached is not None:
        return cached
    img = cv2.imread(str(BODY_PHOTO), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"flute body photo missing: {BODY_PHOTO}")
    rgb = img[:, :, ::-1]
    crop = rgb[CROP_Y0:CROP_Y1, CROP_X0:CROP_X1]
    alpha = _photo_mask(crop)
    _BODY_CACHE["body"] = (crop, alpha)
    return crop, alpha


def _make_plume(color):
    """Pre-rendered breath plume; bottom point sits on the embouchure."""
    w, h = 430, 540
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k in range(64):
        t = k / 63.0
        x = 45 + 345 * t
        y = 500 - 455 * t - 42 * math.sin(math.pi * t) + 13 * math.sin(12.0 * t)
        r = 20 + 40 * t
        a = int(150 * (1.0 - 0.40 * t))
        d.ellipse((x - r, y - r, x + r, y + r), fill=color + (a,))
    img = img.filter(ImageFilter.GaussianBlur(20))
    return np.asarray(img, dtype=np.uint8)


def make_flute_base(palette_name):
    pal = palette(palette_name)
    accent = np.array(pal["accent"], dtype=np.float32)
    crop, alpha = _body_photo()

    rgb = crop.astype(np.float32) / 255.0
    rgb = np.clip(rgb, 0, 1) ** 0.96
    gray = rgb.mean(axis=2, keepdims=True)
    rgb = np.clip(gray + (rgb - gray) * 1.02, 0, 1)
    body = np.dstack([np.clip(rgb * 255.0 + accent[None, None, :] * 0.022,
                              0, 255).astype(np.uint8),
                      np.clip(alpha, 0, 255).astype(np.uint8)])
    flute = Image.fromarray(body, "RGBA")

    # Soft shadow under the foot, then the instrument on top.
    canvas = Image.new("RGBA", (CROP_W, CROP_H), (0, 0, 0, 0))
    sh = Image.new("RGBA", (CROP_W, CROP_H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((175, 3540, 380, 3735), fill=(0, 0, 0, 150))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(28)))
    canvas.alpha_composite(flute)
    return np.array(canvas, dtype=np.uint8)


class FluteStripGenerator:
    def __init__(self, section, notes, seed=0):
        pal = palette(section["palette"])
        self.key_color = np.array(pal["key_glow"], dtype=np.float32)
        self.accent = np.array(pal["accent"], dtype=np.float32)
        self.base = make_flute_base(section["palette"])
        self.key_halo = _glow_rgba(110, self.key_color)
        self.key_ring = _ring_rgba(140, 40.0, 10.0, self.key_color)
        self.tube_light = _capsule_rgba(80, 340, 10.0, 145.0, self.key_color)
        self.tube_core = _capsule_rgba(60, 300, 3.0, 115.0, self.accent)
        self.emb_glow = _glow_rgba(160, self.key_color)
        self.emb_ring = _ring_rgba(110, 34.0, 6.0, self.key_color)
        self.spark = make_spark_sprite(18)
        plume_color = tuple(int(v) for v in (self.accent * 0.35 + self.key_color * 0.65))
        self.plume = _make_plume(plume_color)
        R = cv2.getRotationMatrix2D((INSTR_CX, INSTR_CY), ANGLE, 1.0)
        M = np.array([[SCALE, 0.0, 0.0], [0.0, SCALE, 0.0]]) @ np.vstack([R, [0, 0, 1]])
        M[0, 2] += CENTER_X - SCALE * INSTR_CX
        M[1, 2] += CENTER_Y - SCALE * INSTR_CY
        self.M = M

        self.energy = np.zeros(N_KEYS, dtype=np.float32)
        self.flash = np.zeros(N_KEYS, dtype=np.float32)
        self.emb_energy = 0.0
        self.last_meta = [(67, False)] * N_KEYS

        self.badge_sprites = {}
        self.badge_key = None
        self.badge_alpha = 0.0
        self.badge_since = -10.0
        self.badge_hold = 0.32

        self.section = section
        self.rng = random.Random(seed * 7919 + section["index"] * 104729)

        sec_start, sec_end = section["start"], section["end"]
        self.notes = [n for n in notes
                      if n["start"] <= sec_end and n["end"] >= sec_start - 0.4]
        self.flute_notes = [n for n in self.notes if n["hand"] == "R"]
        self.frame_events = {}
        for n in self.flute_notes:
            fr = int(round((n["start"] - sec_start) * FPS))
            if 0 <= fr < section["frames"]:
                self.frame_events.setdefault(fr, []).append(n)
        self.particles = []

    # ------------------------------------------------------------------
    def frames(self):
        sec = self.section
        dt = 1.0 / FPS
        for i in range(sec["frames"]):
            t = sec["start"] + i * dt
            layer = self.base.copy()
            self._on_onsets(i, t)
            self._decay(dt)
            self._draw_flute(layer, t)
            self._update_particles(layer, dt)
            frame = cv2.warpAffine(layer, self.M, (W, H), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_CONSTANT,
                                   borderValue=(0, 0, 0, 0))
            self._update_badge(t)
            self._draw_badge(frame)
            yield frame.tobytes()

    # ------------------------------------------------------------------
    def _key_for_pitch(self, pitch):
        t = (max(60.0, min(91.0, float(pitch))) - 60.0) / 31.0
        return int(round((1.0 - t) * (N_KEYS - 1)))

    def _on_onsets(self, frame_index, t):
        for n in self.frame_events.get(frame_index, ()):
            si = self._key_for_pitch(n["pitch"])
            self.energy[si] = min(1.3, self.energy[si] + 0.6 + n["vel"] / 190.0)
            self.flash[si] = 1.0
            self.emb_energy = min(1.2, self.emb_energy + 0.55 + n["vel"] / 240.0)
            self.last_meta[si] = (int(n["pitch"]), bool(n.get("arp", False)))
            self._spawn(si, n)

    def _decay(self, dt):
        self.energy *= math.exp(-dt / 0.9)
        self.flash *= math.exp(-dt / 0.12)
        self.emb_energy *= math.exp(-dt / 0.55)
        self.energy[self.energy < 0.004] = 0.0
        self.flash[self.flash < 0.02] = 0.0
        if self.emb_energy < 0.004:
            self.emb_energy = 0.0

    # ------------------------------------------------------------------
    def _draw_flute(self, layer, t):
        # Breath plume from the embouchure.
        if self.emb_energy > 0.02:
            a = min(0.92, 0.40 + 0.55 * self.emb_energy)
            bob = int(round(math.sin(t * 1.7) * 6))
            self._blend_rgba(layer, self.plume, int(EMB_X - 40),
                             int(EMB_Y - 500 + bob), a)
            self._blend_rgba(layer, self.emb_glow, int(EMB_X - 90),
                             int(EMB_Y - 90), 0.65 * self.emb_energy)
            self._blend_rgba(layer, self.emb_ring, int(EMB_X - 55),
                             int(EMB_Y - 55), 0.90 * self.emb_energy)
            self._stamp(layer, EMB_X, EMB_Y, self.spark, self.accent,
                        0.85 * self.emb_energy)

        for si in range(N_KEYS):
            e = float(self.energy[si])
            if e <= 0.02:
                continue
            x, y = KEY_X, KEY_Y[si]
            self._blend_rgba(layer, self.tube_light, int(x - 40), int(y - 170),
                             0.88 * min(1.0, e))
            self._blend_rgba(layer, self.tube_core, int(x - 30), int(y - 150),
                             0.95 * min(1.0, e))
            self._blend_rgba(layer, self.key_halo, int(x - 55), int(y - 55),
                             0.22 * min(1.0, e))
            self._blend_rgba(layer, self.key_ring, int(x - 70), int(y - 70),
                             0.82 * min(1.0, e))
            fl = float(self.flash[si])
            if fl > 0.03:
                self._stamp(layer, x, y, self.spark, self.accent, 0.85 * fl)

    def _stamp(self, layer, cx, cy, sprite, color, intensity):
        sh = sprite.shape[0]
        x0, y0 = int(round(cx - sh / 2)), int(round(cy - sh / 2))
        cx0, cy0 = max(0, x0), max(0, y0)
        cx1, cy1 = min(CROP_W, x0 + sh), min(CROP_H, y0 + sh)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        sub = sprite[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0][:, :, None]
        add = (sub * color[None, None, :] * (255.0 * min(1.0, intensity))).astype(np.uint16)
        _clip_add(layer[cy0:cy1, cx0:cx1, :3], add)
        a = np.clip(sub[:, :, 0] * 255.0 * min(1.0, intensity * 0.9),
                    0, 255).astype(np.uint8)
        layer[cy0:cy1, cx0:cx1, 3] = np.maximum(
            layer[cy0:cy1, cx0:cx1, 3], a)

    def _blend_rgba(self, layer, sprite, x, y, alpha_mul):
        sh, sw = sprite.shape[:2]
        cx0, cy0 = max(0, x), max(0, y)
        cx1, cy1 = min(CROP_W, x + sw), min(CROP_H, y + sh)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        sub = sprite[cy0 - y:cy1 - y, cx0 - x:cx1 - x].astype(np.float32) / 255.0
        a = sub[:, :, 3:4] * alpha_mul
        roi = layer[cy0:cy1, cx0:cx1].astype(np.float32) / 255.0
        rgb = roi[:, :, :3] * (1.0 - a) + sub[:, :, :3] * a
        layer[cy0:cy1, cx0:cx1, :3] = (rgb * 255.0).astype(np.uint8)
        layer[cy0:cy1, cx0:cx1, 3] = np.maximum(
            layer[cy0:cy1, cx0:cx1, 3],
            (a[:, :, 0] * 255.0).astype(np.uint8))

    # ------------------------------------------------------------------
    def _spawn(self, si, note):
        count = 2 + int(note["vel"] / 70)
        x = EMB_X + self.rng.uniform(-4, 4)
        y = EMB_Y + self.rng.uniform(-3, 4)
        for _ in range(count):
            self.particles.append({
                "x": x,
                "y": y,
                "vx": self.rng.uniform(8, 42),
                "vy": self.rng.uniform(-45, -10),
                "life": self.rng.uniform(0.35, 0.9),
                "age": 0.0,
            })

    def _update_particles(self, layer, dt):
        alive = []
        sprite = self.spark
        sh = sprite.shape[0]
        for p in self.particles:
            p["age"] += dt
            if p["age"] >= p["life"]:
                continue
            p["x"] += p["vx"] * dt
            p["vy"] += 8 * dt
            p["y"] += p["vy"] * dt
            life = 1.0 - p["age"] / p["life"]
            x, y = int(p["x"]), int(p["y"])
            x0, y0 = x - sh // 2, y - sh // 2
            x1, y1 = x0 + sh, y0 + sh
            if x1 <= 0 or y1 <= 0 or x0 >= CROP_W or y0 >= CROP_H:
                continue
            cx0, cy0 = max(0, x0), max(0, y0)
            cx1, cy1 = min(CROP_W, x1), min(CROP_H, y1)
            sub = sprite[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
            add = (sub[:, :, None] * self.accent[None, None, :]
                   * (life ** 1.4) * 185).astype(np.uint16)
            _clip_add(layer[cy0:cy1, cx0:cx1, :3], add)
            a = np.clip(sub * (life ** 1.3) * 220, 0, 255).astype(np.uint8)
            layer[cy0:cy1, cx0:cx1, 3] = np.maximum(
                layer[cy0:cy1, cx0:cx1, 3], a)
            alive.append(p)
        self.particles = alive

    # ------------------------------------------------------------------
    def _badge_sprite(self, pitch, arp):
        key = (pitch, arp)
        sprite = self.badge_sprites.get(key)
        if sprite is not None:
            return sprite
        note = f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"
        detail = "ornament" if arp else "open"
        f_note = load_font("sans", 14)
        f_detail = load_font("sans", 11)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        nw = int(probe.textlength(note, font=f_note))
        dw = int(probe.textlength(detail, font=f_detail))
        pad_x, gap, h = 10, 8, 29
        w = pad_x * 2 + nw + gap + dw
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=8, fill=(7, 10, 17, 214),
                            outline=tuple(int(c) for c in self.accent) + (105,), width=1)
        d.text((pad_x, 6), note, font=f_note, fill=(236, 243, 255, 240))
        d.text((pad_x + nw + gap, 9), detail, font=f_detail,
               fill=(204, 214, 232, 225))
        arr = np.asarray(img, dtype=np.float32)
        sprite = (arr[:, :, :3], arr[:, :, 3] / 255.0, w, h)
        self.badge_sprites[key] = sprite
        return sprite

    def _update_badge(self, t):
        loudest = int(np.argmax(self.energy))
        if self.energy[loudest] > 0.05:
            pitch, arp = self.last_meta[loudest]
            key = (pitch, loudest, arp)
            if key != self.badge_key and t - self.badge_since >= self.badge_hold:
                self.badge_key = key
                self.badge_since = t
            target = 1.0
        else:
            target = 0.0
        self.badge_alpha += (target - self.badge_alpha) * 0.18

    def _draw_badge(self, frame):
        if self.badge_key is None or self.badge_alpha < 0.03:
            return
        pitch, si, arp = self.badge_key
        rgb, alpha, w, h = self._badge_sprite(pitch, arp)
        sx, sy = self._project(KEY_X, KEY_Y[si])
        x = int(round(sx + 20))
        y = int(round(sy + 14))
        if (x + w > PANEL_X - 6 and x < PANEL_X + PANEL_W + 6
                and y + h > PANEL_Y - 6 and y < PANEL_Y + PANEL_H + 6):
            x = int(round(sx - w - 20))
        x = max(16, min(W - w - 16, x))
        y = max(16, min(H - h - 16, y))
        self._blend(frame, x, y, rgb, alpha, min(1.0, self.badge_alpha))

    def _project(self, x, y):
        return (self.M[0, 0] * x + self.M[0, 1] * y + self.M[0, 2],
                self.M[1, 0] * x + self.M[1, 1] * y + self.M[1, 2])

    def _blend(self, frame, x, y, rgb, alpha, mul=1.0):
        sh, sw = alpha.shape
        cx0, cy0 = max(0, x), max(0, y)
        cx1, cy1 = min(W, x + sw), min(H, y + sh)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        a = (alpha[cy0 - y:cy1 - y, cx0 - x:cx1 - x] * mul)[:, :, None]
        roi = frame[cy0:cy1, cx0:cx1]
        roi[:, :, :3] = np.clip(roi[:, :, :3] * (1.0 - a)
                                + rgb[cy0 - y:cy1 - y, cx0 - x:cx1 - x] * a,
                                0, 255).astype(np.uint8)
        roi[:, :, 3] = np.maximum(roi[:, :, 3], (a[:, :, 0] * 255).astype(np.uint8))


class PipeWriter:
    """Writes frames to a subprocess stdin on a background thread."""

    def __init__(self, stream, maxsize=4):
        self.stream = stream
        self.q = queue.Queue(maxsize=maxsize)
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def write(self, data):
        while not self.error:
            try:
                self.q.put(data, timeout=0.5)
                return
            except queue.Full:
                continue

    def close(self):
        while True:
            try:
                self.q.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        self.thread.join()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            if self.error is not None:
                continue
            try:
                self.stream.write(item)
            except (BrokenPipeError, ValueError, OSError) as exc:
                self.error = exc


def stream_section(section, notes, stream, seed=0):
    gen = FluteStripGenerator(section, notes, seed)
    writer = PipeWriter(stream)
    try:
        for frame in gen.frames():
            writer.write(frame)
    finally:
        writer.close()
        try:
            stream.close()
        except Exception:
            pass


if __name__ == "__main__":
    import time

    out = Path(__file__).resolve().parents[1] / "_src"
    out.mkdir(parents=True, exist_ok=True)
    section = {"index": 0, "name": "I. Aurora", "palette": "midnight",
               "start": 0.0, "end": 6.0, "frames": 180, "bpm": 64}
    notes = []
    for k, pitch in enumerate((62, 67, 71, 74, 79, 83)):
        notes.append({"start": 0.6 + k * 0.32, "end": 2.2 + k * 0.3,
                      "pitch": pitch, "vel": 84, "hand": "R", "arp": False})
    notes.append({"start": 3.4, "end": 5.2, "pitch": 86, "vel": 96,
                  "hand": "R", "arp": True})
    notes.append({"start": 0.4, "end": 4.0, "pitch": 40, "vel": 58,
                  "hand": "L", "arp": False})

    t0 = time.time()
    gen = FluteStripGenerator(section, notes, 7)
    picks = {8: "flute_frame_01.png", 30: "flute_frame_02.png",
             60: "flute_frame_03.png", 120: "flute_frame_04.png"}
    bg = Image.new("RGBA", (W, H), (8, 12, 26, 255))
    for i, raw in enumerate(gen.frames()):
        if i in picks:
            layer = Image.frombytes("RGBA", (W, H), raw)
            img = Image.alpha_composite(bg, layer)
            img.save(out / picks[i])
    print("rendered 180 frames in %.1fs" % (time.time() - t0))
