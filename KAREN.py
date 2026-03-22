#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  K.A.R.E.N. HUD — THE SPIDER-SENSE SENTRY (ESP32-CAM Edition)              ║
║                                                                            ║
║  Trigger sources:                                                          ║
║    1. CLAP / audio anomaly — Master ESP32 sends [TINGLE] → auto-capture   ║
║    2. SPACEBAR or on-screen FIRE button — sends [CAPTURE] over Serial      ║
║    3. Type [CAPTURE] in PC terminal directly into Serial Monitor            ║
║                                                                            ║
║  Usage:                                                                    ║
║    python KAREN.py --port COMx                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import argparse
import threading
import serial
import serial.tools.list_ports
import pygame
import cv2
import numpy as np

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
WEB_TETHER_BAUD = 921600
WINDOW_WIDTH    = 1280
WINDOW_HEIGHT   = 800
FPS             = 60

COLOR_BG             = (8, 12, 20)
COLOR_BG_PANEL       = (13, 20, 38)
COLOR_BG_PANEL_DARK  = (8, 14, 26)
COLOR_GRID           = (20, 32, 58)
COLOR_TEXT           = (200, 225, 255)
COLOR_TEXT_DIM       = (80, 110, 155)
COLOR_TEXT_BRIGHT    = (240, 248, 255)

COLOR_SPIDER_RED     = (220, 25, 35)
COLOR_SPIDER_BLUE    = (15, 70, 230)
COLOR_WEB_GOLD       = (255, 200, 40)
COLOR_THREAT_CRIT    = (255, 10, 20)
COLOR_THREAT_HIGH    = (255, 100, 0)
COLOR_THREAT_MED     = (255, 200, 0)
COLOR_THREAT_SECURE  = (0, 220, 120)
COLOR_BORDER         = (30, 50, 90)
COLOR_BORDER_BRIGHT  = (50, 80, 140)
COLOR_TRIGGER_BTN    = (180, 20, 30)
COLOR_TRIGGER_HOV    = (230, 40, 50)
COLOR_TRIGGER_PRESS  = (255, 80, 80)

FFT_BAR_COUNT    = 64
FFT_PANEL_HEIGHT = 130
FFT_MAX_VALUE    = 2500

TRIGGER_LOG_MAX  = 6   # lines shown in trigger log panel

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED STATE
# ═══════════════════════════════════════════════════════════════════════════════
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.fft_bins = [0] * FFT_BAR_COUNT
        self.camera_surface = None
        self.threat_string = "WAITING"
        self.threat_level = -1
        self.faces_found = 0
        self.serial_connected = False
        self.ser_ref = None
        self.last_capture_time = 0
        self.capture_count = 0
        self.trigger_log = []          # list of (timestamp_str, message, color)
        self.tingle_flash = 0          # countdown frames for tingle flash effect
        self.last_frame_size = 0
        self.last_frame_time_ms = 0

    def _add_log(self, msg, color=None):
        """Internal — call with lock held."""
        if color is None:
            color = COLOR_TEXT_DIM
        ts = time.strftime("%H:%M:%S")
        self.trigger_log.append((ts, msg, color))
        if len(self.trigger_log) > TRIGGER_LOG_MAX:
            self.trigger_log.pop(0)

    def update_fft(self, bins):
        with self.lock:
            self.fft_bins = bins[:]

    def update_camera_jpeg(self, jpeg_bytes, rx_time_ms=0):
        t0 = time.time()
        try:
            nparr = np.frombuffer(jpeg_bytes, np.uint8)
            img_np = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img_np is not None:
                frame_rgb = cv2.cvtColor(img_np, cv2.COLOR_BGR2RGB)
                surface = pygame.surfarray.make_surface(frame_rgb.swapaxes(0, 1))
                with self.lock:
                    self.camera_surface = surface
                    self.last_frame_size = len(jpeg_bytes)
                    self.last_frame_time_ms = rx_time_ms
                    self._add_log(f"Frame recv: {len(jpeg_bytes)//1024:.1f} KB  {rx_time_ms} ms", COLOR_THREAT_SECURE)
        except Exception as e:
            with self.lock:
                self._add_log(f"JPEG decode error: {e}", COLOR_THREAT_CRIT)

    def update_threat(self, level, faces):
        with self.lock:
            self.threat_level = level
            self.faces_found = faces
            labels = {-1:"ERROR", 0:"SECURE", 1:"GUARDED", 2:"ELEVATED",
                      3:"HIGH", 4:"SEVERE", 5:"CRITICAL"}
            self.threat_string = labels.get(level, "UNKNOWN")
            color = COLOR_THREAT_CRIT if level >= 4 else \
                    COLOR_THREAT_HIGH if level == 3 else \
                    COLOR_THREAT_MED  if level == 2 else \
                    COLOR_THREAT_SECURE
            self._add_log(f"Threat → {self.threat_string}  faces={faces}", color)

    def trigger_capture(self, source="MANUAL"):
        """Send [CAPTURE] to the Master ESP32."""
        with self.lock:
            if self.ser_ref and self.serial_connected:
                try:
                    self.ser_ref.write(b"[CAPTURE]\n")
                    self.capture_count += 1
                    self.last_capture_time = time.time()
                    self._add_log(f"[CAPTURE] sent via {source}", COLOR_WEB_GOLD)
                    return True
                except Exception as e:
                    self._add_log(f"Send failed: {e}", COLOR_THREAT_CRIT)
                    return False
            else:
                self._add_log("No serial — trigger ignored", COLOR_THREAT_CRIT)
                return False

    def mark_tingle(self):
        with self.lock:
            self.tingle_flash = 45
            self._add_log("SPIDER-SENSE TINGLE!", COLOR_SPIDER_RED)

    def get_data(self):
        with self.lock:
            return {
                "fft": self.fft_bins[:],
                "cam": self.camera_surface,
                "threat": self.threat_string,
                "threat_level": self.threat_level,
                "faces": self.faces_found,
                "conn": self.serial_connected,
                "cap_count": self.capture_count,
                "last_cap": self.last_capture_time,
                "log": list(self.trigger_log),
                "tingle_flash": self.tingle_flash,
                "frame_size": self.last_frame_size,
                "frame_time": self.last_frame_time_ms,
            }

    def tick_tingle(self):
        with self.lock:
            if self.tingle_flash > 0:
                self.tingle_flash -= 1

# ═══════════════════════════════════════════════════════════════════════════════
# THREAD: Serial Parser
# ═══════════════════════════════════════════════════════════════════════════════
class SerialListener(threading.Thread):
    def __init__(self, port, baud, shared_state):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.state = shared_state
        self.running = True

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.05)
            with self.state.lock:
                self.state.serial_connected = True
                self.state.ser_ref = ser
                self.state._add_log(f"Serial {self.port} @ {self.baud}", COLOR_THREAT_SECURE)
            print(f"[TETHER] Connected: {self.port} @ {self.baud} baud")
        except Exception as e:
            print(f"[TETHER] ERROR: {e}")
            with self.state.lock:
                self.state._add_log(f"Serial failed: {e}", COLOR_THREAT_CRIT)
            return

        buf = bytearray()
        frame_rx_start = 0

        while self.running:
            try:
                waiting = ser.in_waiting
                if waiting > 0:
                    buf.extend(ser.read(min(waiting, 4096)))
                else:
                    chunk = ser.read(1)
                    if chunk:
                        buf.extend(chunk)

                # ── Process all complete packets ──────────────────────────
                while True:
                    frame_idx = buf.find(b'[FRAME_S]')
                    nl_idx    = buf.find(b'\n')

                    has_frame = frame_idx >= 0
                    has_line  = nl_idx >= 0

                    # Binary frame takes priority if it starts before next newline
                    if has_frame and (not has_line or frame_idx < nl_idx):
                        need = frame_idx + 9 + 4     # marker + length bytes
                        if len(buf) < need:
                            break                    # wait for length bytes

                        jpeg_len = int.from_bytes(buf[frame_idx+9:frame_idx+13], 'big')

                        if jpeg_len == 0 or jpeg_len > 200_000:
                            buf = buf[frame_idx+9:]  # skip bad marker
                            continue

                        total_need = frame_idx + 13 + jpeg_len + 9
                        if len(buf) < total_need:
                            break                    # wait for rest of JPEG

                        jpeg_data  = bytes(buf[frame_idx+13 : frame_idx+13+jpeg_len])
                        end_marker = bytes(buf[frame_idx+13+jpeg_len : frame_idx+13+jpeg_len+9])

                        rx_ms = int((time.time() - frame_rx_start) * 1000) if frame_rx_start else 0

                        if end_marker == b'[FRAME_E]':
                            self.state.update_camera_jpeg(jpeg_data, rx_ms)
                        else:
                            with self.state.lock:
                                self.state._add_log("Frame end-marker mismatch", COLOR_THREAT_CRIT)

                        buf = buf[frame_idx+13+jpeg_len+9:]
                        frame_rx_start = 0
                        continue

                    elif has_line:
                        raw_line = buf[:nl_idx]
                        buf = buf[nl_idx+1:]
                        line = raw_line.decode('utf-8', errors='ignore').strip()
                        if not line:
                            continue

                        # ── [FRAME_S] appears as a text-like prefix: mark start time ──
                        if b'[FRAME_S]' in raw_line:
                            frame_rx_start = time.time()

                        if line == "[TINGLE]":
                            self.state.mark_tingle()
                            # Auto-trigger capture on tingle (mirrors Task_Karen behavior)
                            # Master already sends [CAPTURE] to CAM on tingle,
                            # so we just log it here rather than double-triggering.
                            print("[K.A.R.E.N.] TINGLE!")

                        elif line.startswith("[FFT]") and "[/FFT]" in line:
                            csv = line[5:line.index("[/FFT]")]
                            try:
                                raw = [int(x) for x in csv.split(",") if x.strip()]
                                if len(raw) >= FFT_BAR_COUNT:
                                    step = len(raw) // FFT_BAR_COUNT
                                    bars = [sum(raw[i*step:(i+1)*step])//step
                                            for i in range(FFT_BAR_COUNT)]
                                    self.state.update_fft(bars)
                            except:
                                pass

                        elif line.startswith("[CAM_THREAT]"):
                            # [CAM_THREAT] level=X faces=Y
                            level, faces = 0, 0
                            for part in line.split():
                                if part.startswith("level="): level = int(part.split("=")[1])
                                if part.startswith("faces="): faces = int(part.split("=")[1])
                            self.state.update_threat(level, faces)

                        else:
                            print(f"ESP32> {line}")

                        continue

                    else:
                        break   # nothing complete yet

            except Exception as e:
                print(f"[TETHER] Exception: {e}")
                time.sleep(0.2)

        try:
            ser.close()
        except:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# PYGAME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def draw_panel(surf, rect, bg=COLOR_BG_PANEL, border=COLOR_BORDER, radius=4):
    pygame.draw.rect(surf, bg, rect, border_radius=radius)
    pygame.draw.rect(surf, border, rect, 1, border_radius=radius)

def draw_label(surf, font, text, x, y, color=COLOR_TEXT_DIM):
    s = font.render(text, True, color)
    surf.blit(s, (x, y))
    return s.get_width()

def draw_scanlines(surf, rect, alpha=18):
    """Subtle CRT scanline effect over a rect."""
    for y in range(rect.top, rect.bottom, 3):
        pygame.draw.line(surf, (0,0,0), (rect.left, y), (rect.right, y))

def threat_color(level):
    if level >= 5: return COLOR_THREAT_CRIT
    if level == 4: return COLOR_THREAT_CRIT
    if level == 3: return COLOR_THREAT_HIGH
    if level == 2: return COLOR_THREAT_MED
    if level == 0: return COLOR_THREAT_SECURE
    return COLOR_TEXT_DIM

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════
class SentryDisplay:
    def __init__(self, shared_state):
        self.state = shared_state
        self.smooth_fft = [0.0] * FFT_BAR_COUNT
        self.frame_count = 0
        self.btn_hover = False
        self.btn_press_frames = 0   # flash effect after click

    def run(self):
        pygame.init()
        screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("K.A.R.E.N. — Spider-Sense Sentry HUD")
        clock = pygame.time.Clock()

        # Fonts — monospace for the tactical HUD feel
        try:
            font_huge  = pygame.font.SysFont("Courier New", 44, bold=True)
            font_large = pygame.font.SysFont("Courier New", 28, bold=True)
            font_med   = pygame.font.SysFont("Courier New", 17, bold=True)
            font_small = pygame.font.SysFont("Courier New", 13)
            font_tiny  = pygame.font.SysFont("Courier New", 11)
        except:
            font_huge  = pygame.font.SysFont("monospace", 44, bold=True)
            font_large = pygame.font.SysFont("monospace", 28, bold=True)
            font_med   = pygame.font.SysFont("monospace", 17, bold=True)
            font_small = pygame.font.SysFont("monospace", 13)
            font_tiny  = pygame.font.SysFont("monospace", 11)

        # ── Layout constants ─────────────────────────────────────────────────
        HEADER_H  = 44
        FOOTER_H  = FFT_PANEL_HEIGHT
        BODY_TOP  = HEADER_H + 6
        BODY_H    = WINDOW_HEIGHT - HEADER_H - FOOTER_H - 12
        BODY_BOT  = BODY_TOP + BODY_H

        CAM_W     = int(WINDOW_WIDTH * 0.62)
        CAM_H     = BODY_H
        CAM_RECT  = pygame.Rect(6, BODY_TOP, CAM_W, CAM_H)

        SIDE_X    = CAM_RECT.right + 6
        SIDE_W    = WINDOW_WIDTH - SIDE_X - 6
        SIDE_H    = BODY_H

        # Side panel subdivision
        THREAT_H  = 160
        STATUS_H  = 90
        BTN_H     = 72
        LOG_H     = SIDE_H - THREAT_H - STATUS_H - BTN_H - 12

        THREAT_RECT = pygame.Rect(SIDE_X, BODY_TOP,          SIDE_W, THREAT_H)
        STATUS_RECT = pygame.Rect(SIDE_X, BODY_TOP+THREAT_H+4, SIDE_W, STATUS_H)
        BTN_RECT    = pygame.Rect(SIDE_X, STATUS_RECT.bottom+4, SIDE_W, BTN_H)
        LOG_RECT    = pygame.Rect(SIDE_X, BTN_RECT.bottom+4,  SIDE_W, LOG_H)

        FFT_RECT  = pygame.Rect(6, BODY_BOT+6, WINDOW_WIDTH-12, FOOTER_H-12)

        running = True
        while running:
            self.frame_count += 1
            self.state.tick_tingle()

            mx, my = pygame.mouse.get_pos()
            self.btn_hover = BTN_RECT.collidepoint(mx, my)
            if self.btn_press_frames > 0:
                self.btn_press_frames -= 1

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_SPACE, pygame.K_RETURN):
                        self.state.trigger_capture("KEYBOARD")
                        self.btn_press_frames = 12

                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1 and self.btn_hover:
                        self.state.trigger_capture("BUTTON")
                        self.btn_press_frames = 12

            data = self.state.get_data()
            screen.fill(COLOR_BG)

            # ── Draw background grid dots ─────────────────────────────────
            for gx in range(0, WINDOW_WIDTH, 32):
                for gy in range(0, WINDOW_HEIGHT, 32):
                    pygame.draw.circle(screen, COLOR_GRID, (gx, gy), 1)

            # ══════════════════════════════════════════════════════════════
            # HEADER
            # ══════════════════════════════════════════════════════════════
            pygame.draw.rect(screen, COLOR_BG_PANEL_DARK, (0, 0, WINDOW_WIDTH, HEADER_H))
            pygame.draw.line(screen, COLOR_SPIDER_RED, (0, HEADER_H-1), (WINDOW_WIDTH, HEADER_H-1), 1)

            title = font_large.render("⬡  K.A.R.E.N.  SPIDER-SENSE SENTRY", True, COLOR_TEXT_BRIGHT)
            screen.blit(title, (12, (HEADER_H - title.get_height()) // 2))

            conn_color = COLOR_THREAT_SECURE if data["conn"] else COLOR_THREAT_CRIT
            conn_text  = "● TETHER LIVE" if data["conn"] else "● NO TETHER"
            cs = font_small.render(conn_text, True, conn_color)
            screen.blit(cs, (WINDOW_WIDTH - cs.get_width() - 12, (HEADER_H - cs.get_height())//2))

            ts_txt = font_tiny.render(time.strftime("%H:%M:%S"), True, COLOR_TEXT_DIM)
            screen.blit(ts_txt, (WINDOW_WIDTH - cs.get_width() - ts_txt.get_width() - 24,
                                  (HEADER_H - ts_txt.get_height())//2))

            # ══════════════════════════════════════════════════════════════
            # CAMERA PANEL
            # ══════════════════════════════════════════════════════════════
            tingle_active = data["tingle_flash"] > 0
            cam_border_color = COLOR_SPIDER_RED if tingle_active and (self.frame_count % 6 < 3) \
                               else COLOR_BORDER_BRIGHT if data["cam"] else COLOR_BORDER

            draw_panel(screen, CAM_RECT, bg=COLOR_BG_PANEL_DARK, border=cam_border_color, radius=3)

            if data["cam"]:
                cw, ch = data["cam"].get_size()
                scale  = min((CAM_RECT.width  - 4) / cw,
                             (CAM_RECT.height - 4) / ch)
                sw, sh = int(cw * scale), int(ch * scale)
                scaled = pygame.transform.smoothscale(data["cam"], (sw, sh))
                cx = CAM_RECT.x + (CAM_RECT.width  - sw) // 2
                cy = CAM_RECT.y + (CAM_RECT.height - sh) // 2
                screen.blit(scaled, (cx, cy))

                # Crosshair
                mid_x = cx + sw // 2
                mid_y = cy + sh // 2
                pygame.draw.line(screen, (*COLOR_SPIDER_RED, 100),
                                 (mid_x, cy), (mid_x, cy + sh), 1)
                pygame.draw.line(screen, (*COLOR_SPIDER_RED, 100),
                                 (cx, mid_y), (cx + sw, mid_y), 1)
                pygame.draw.circle(screen, COLOR_SPIDER_RED, (mid_x, mid_y), 12, 1)
                pygame.draw.circle(screen, COLOR_SPIDER_RED, (mid_x, mid_y), 4,  1)

                # Corner brackets
                L = 18
                for bx, by, sx, sy in [(cx,cy,1,1),(cx+sw,cy,-1,1),(cx,cy+sh,1,-1),(cx+sw,cy+sh,-1,-1)]:
                    pygame.draw.line(screen, COLOR_WEB_GOLD, (bx, by), (bx+sx*L, by), 2)
                    pygame.draw.line(screen, COLOR_WEB_GOLD, (bx, by), (bx, by+sy*L), 2)

                # Frame metadata overlay
                if data["frame_size"] > 0:
                    meta = font_tiny.render(
                        f"{data['frame_size']//1024:.1f} KB  {data['frame_time']} ms",
                        True, COLOR_TEXT_DIM)
                    screen.blit(meta, (CAM_RECT.x + 6, CAM_RECT.bottom - meta.get_height() - 6))

            else:
                # No image yet — instructions
                lines = [
                    "NO IMAGE RECEIVED",
                    "",
                    "Trigger sources:",
                    "  [SPACE] or ENTER   — keyboard",
                    "  Click FIRE button  — on-screen",
                    "  Clap near mic      — audio tingle",
                    "  Serial: [CAPTURE]  — terminal",
                ]
                for i, ln in enumerate(lines):
                    color = COLOR_TEXT if i == 0 else COLOR_TEXT_DIM
                    s = font_med.render(ln, True, color) if i < 2 else font_small.render(ln, True, color)
                    screen.blit(s, (CAM_RECT.centerx - s.get_width()//2,
                                   CAM_RECT.centery - 60 + i * 22))

            # ══════════════════════════════════════════════════════════════
            # THREAT PANEL
            # ══════════════════════════════════════════════════════════════
            draw_panel(screen, THREAT_RECT)
            draw_label(screen, font_tiny, "THREAT ASSESSMENT", THREAT_RECT.x+10, THREAT_RECT.y+8,
                       COLOR_TEXT_DIM)

            tlvl  = data["threat_level"]
            tcol  = threat_color(tlvl)
            tstr  = data["threat"]

            # Big threat level number
            lvl_num = font_huge.render(str(max(tlvl, 0)), True, tcol)
            screen.blit(lvl_num, (THREAT_RECT.x + 10, THREAT_RECT.y + 24))

            # Threat label
            tlbl = font_large.render(tstr, True, tcol)
            screen.blit(tlbl, (THREAT_RECT.x + lvl_num.get_width() + 18, THREAT_RECT.y + 34))

            # Threat bar
            bar_rect = pygame.Rect(THREAT_RECT.x+10, THREAT_RECT.y+90, THREAT_RECT.width-20, 12)
            pygame.draw.rect(screen, COLOR_BORDER, bar_rect, border_radius=2)
            if tlvl > 0:
                fill_w = int((tlvl / 5) * bar_rect.width)
                pygame.draw.rect(screen, tcol,
                                 pygame.Rect(bar_rect.x, bar_rect.y, fill_w, bar_rect.height),
                                 border_radius=2)

            # Faces
            faces_s = font_med.render(f"TARGETS: {data['faces']}", True, COLOR_TEXT)
            screen.blit(faces_s, (THREAT_RECT.x+10, THREAT_RECT.y+112))

            # Threat level labels below bar
            for i, lbl in enumerate(["0", "1", "2", "3", "4", "5"]):
                px = bar_rect.x + int((i / 5) * bar_rect.width)
                s  = font_tiny.render(lbl, True, COLOR_TEXT_DIM)
                screen.blit(s, (px - s.get_width()//2, bar_rect.bottom + 2))

            # ══════════════════════════════════════════════════════════════
            # STATUS PANEL
            # ══════════════════════════════════════════════════════════════
            draw_panel(screen, STATUS_RECT)
            draw_label(screen, font_tiny, "SYSTEM STATUS", STATUS_RECT.x+10, STATUS_RECT.y+8,
                       COLOR_TEXT_DIM)

            cap_ago = time.time() - data["last_cap"] if data["last_cap"] else None
            cap_str = f"{cap_ago:.1f}s ago" if cap_ago and cap_ago < 3600 else "never"

            lines = [
                (f"CAPTURES SENT:  {data['cap_count']}", COLOR_TEXT),
                (f"LAST TRIGGER:   {cap_str}",           COLOR_TEXT_DIM),
                (f"TETHER:         {'ONLINE' if data['conn'] else 'OFFLINE'}",
                 COLOR_THREAT_SECURE if data["conn"] else COLOR_THREAT_CRIT),
            ]
            for i, (txt, col) in enumerate(lines):
                s = font_small.render(txt, True, col)
                screen.blit(s, (STATUS_RECT.x+10, STATUS_RECT.y+24+i*20))

            # ══════════════════════════════════════════════════════════════
            # FIRE BUTTON
            # ══════════════════════════════════════════════════════════════
            pressing = self.btn_press_frames > 0
            btn_color = COLOR_TRIGGER_PRESS if pressing else \
                        COLOR_TRIGGER_HOV   if self.btn_hover else \
                        COLOR_TRIGGER_BTN

            # Outer glow when hovered
            if self.btn_hover or pressing:
                glow = pygame.Rect(BTN_RECT.x-2, BTN_RECT.y-2,
                                   BTN_RECT.width+4, BTN_RECT.height+4)
                pygame.draw.rect(screen, (*btn_color[:3], 60), glow, border_radius=6)

            pygame.draw.rect(screen, btn_color, BTN_RECT, border_radius=5)
            pygame.draw.rect(screen, COLOR_SPIDER_RED if not pressing else COLOR_WEB_GOLD,
                             BTN_RECT, 2, border_radius=5)

            btn_label = font_large.render("▶  FIRE CAPTURE", True, COLOR_TEXT_BRIGHT)
            bx = BTN_RECT.centerx - btn_label.get_width()//2
            by = BTN_RECT.centery - btn_label.get_height()//2
            if pressing: by += 1
            screen.blit(btn_label, (bx, by))

            hint = font_tiny.render("SPACEBAR / ENTER / CLICK", True, COLOR_TEXT_DIM)
            screen.blit(hint, (BTN_RECT.centerx - hint.get_width()//2, BTN_RECT.bottom - 14))

            # ══════════════════════════════════════════════════════════════
            # TRIGGER LOG
            # ══════════════════════════════════════════════════════════════
            draw_panel(screen, LOG_RECT, bg=COLOR_BG_PANEL_DARK)
            draw_label(screen, font_tiny, "EVENT LOG", LOG_RECT.x+10, LOG_RECT.y+6, COLOR_TEXT_DIM)

            log_y = LOG_RECT.y + 22
            for ts_str, msg, col in data["log"]:
                if log_y + 14 > LOG_RECT.bottom - 4:
                    break
                ts_s  = font_tiny.render(ts_str, True, COLOR_TEXT_DIM)
                msg_s = font_tiny.render(msg,    True, col)
                screen.blit(ts_s,  (LOG_RECT.x+6,  log_y))
                screen.blit(msg_s, (LOG_RECT.x+66, log_y))
                log_y += 15

            # ══════════════════════════════════════════════════════════════
            # FFT WAVEFORM (bottom)
            # ══════════════════════════════════════════════════════════════
            draw_panel(screen, FFT_RECT, bg=COLOR_BG_PANEL_DARK)
            draw_label(screen, font_tiny, "AUDIO SPECTRUM  (clap to trigger)",
                       FFT_RECT.x+8, FFT_RECT.y+4, COLOR_TEXT_DIM)

            bar_area_h = FFT_RECT.height - 22
            bar_w = max(1, (FFT_RECT.width - 8) // FFT_BAR_COUNT - 1)

            for i in range(FFT_BAR_COUNT):
                target = min(data["fft"][i], FFT_MAX_VALUE)
                self.smooth_fft[i] += (target - self.smooth_fft[i]) * 0.25
                ratio = self.smooth_fft[i] / FFT_MAX_VALUE
                h = max(2, int(ratio * bar_area_h))
                bx = FFT_RECT.x + 4 + i * (bar_w + 1)
                by = FFT_RECT.bottom - h - 2

                # Colour shifts from blue → red as energy rises
                r = int(ratio * 220)
                b = int((1 - ratio) * 200) + 30
                pygame.draw.rect(screen, (r, 40, b), (bx, by, bar_w, h))

            # Tingle threshold line
            thresh_y = FFT_RECT.bottom - int((0.35) * bar_area_h) - 2
            pygame.draw.line(screen, (*COLOR_SPIDER_RED, 120),
                             (FFT_RECT.x+4, thresh_y), (FFT_RECT.right-4, thresh_y), 1)
            draw_label(screen, font_tiny, "TINGLE THRESHOLD",
                       FFT_RECT.x+8, thresh_y-12, COLOR_SPIDER_RED)

            # Tingle flash overlay
            if tingle_active and (self.frame_count % 8 < 4):
                overlay = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.SRCALPHA)
                overlay.fill((220, 25, 35, 18))
                screen.blit(overlay, (0, 0))
                msg = font_large.render("! SPIDER-SENSE TINGLE !", True, COLOR_SPIDER_RED)
                screen.blit(msg, (WINDOW_WIDTH//2 - msg.get_width()//2,
                                  BODY_TOP + 10))

            pygame.display.flip()
            clock.tick(FPS)

        pygame.quit()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
def list_ports():
    print("\nAvailable serial ports:")
    for p in serial.tools.list_ports.comports():
        print(f"  {p.device:15s} — {p.description}")
    print()

def main():
    parser = argparse.ArgumentParser(description="K.A.R.E.N. Spider-Sense HUD")
    parser.add_argument("--port", "-p", required=False,
                        help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    args = parser.parse_args()

    if not args.port:
        list_ports()
        args.port = input("Enter port: ").strip()

    shared   = SharedState()
    listener = SerialListener(args.port, WEB_TETHER_BAUD, shared)
    listener.start()

    display = SentryDisplay(shared)
    try:
        display.run()
    finally:
        listener.running = False

    sys.exit(0)

if __name__ == "__main__":
    main()
