import pygame
import sys
import math
from collections import deque

# -------------------- Constants (variant 30) --------------------
ADC_BITS = 8
ADC_VREF = 19.5  # V, input range 0..19.5
ADC_MAX = (1 << ADC_BITS) - 1

DAC_BITS = 8
DAC_VMAX = 120.0  # V, output range 0..120
DAC_MAX = (1 << DAC_BITS) - 1

U_NOM = 116.0
U_HALF = U_NOM * 0.5
U_SLOW = U_NOM * 0.3

CODE_NOM = round(U_NOM / DAC_VMAX * DAC_MAX)   # ~247
CODE_HALF = round(U_HALF / DAC_VMAX * DAC_MAX) # ~123-124
CODE_SLOW = round(U_SLOW / DAC_VMAX * DAC_MAX) # ~74
CODE_ZERO = 0

# Tensometric sensor: 0..120 kgf/mm^2 -> 0..15 V
TENSO_MAX = 120.0
TENSO_U_MAX = 15.0

TH_55 = 55.0
TH_70 = 70.0
TH_95 = 95.0

RAMP_UP_SEC = 12.0    # Fig B.8a
RAMP_DOWN_SEC = 5.0   # Fig B.8b

SAMPLE_PERIOD = 0.25  # like driver step
MAX_LOG_LINES = 12

# Gate kinematics (just for visualization)
# Full travel at 116V takes ~30 seconds
TRAVEL_TIME_AT_NOM = 30.0
BASE_SPEED = 1.0 / TRAVEL_TIME_AT_NOM

OPEN1_POS = 0.80
OPEN2_POS = 1.00
CLOSE1_POS = 0.20
CLOSE2_POS = 0.00

# UI
W, H = 1600, 900

# Bit mapping for visualization (as in your scheme)
# Port 300h (input):
# bit15 GT, bit14 KZ, bit13 KO, bit12 US(OR), bit11 KV_Z2, bit10 KV_Z1, bit9 KV_O2, bit8 KV_O1, bits7..0 ADC
# Port 301h (output):
# bits7..0 DAC, bit14 ZP_DAC, bit15 ZP_ADC

# -------------------- Helpers --------------------
def clamp(x, a, b):
    return a if x < a else b if x > b else x

def tenso_to_voltage(stress):
    return (stress / TENSO_MAX) * TENSO_U_MAX

def adc_code_from_voltage(u):
    # 8-bit, range 0..19.5V
    u = clamp(u, 0.0, ADC_VREF)
    return int(round(u / ADC_VREF * ADC_MAX))

def dac_voltage_from_code(code):
    return (code / DAC_MAX) * DAC_VMAX

def fmt_bool(v):
    return "1" if v else "0"

# -------------------- Ramp controller --------------------
class Ramp:
    def __init__(self):
        self.active = False
        self.start_code = 0
        self.end_code = 0
        self.duration = 0.0
        self.t = 0.0

    def start(self, current_code, target_code, duration):
        self.active = True
        self.start_code = int(current_code)
        self.end_code = int(target_code)
        self.duration = max(1e-6, float(duration))
        self.t = 0.0

    def update(self, dt):
        if not self.active:
            return self.end_code, False
        self.t += dt
        k = clamp(self.t / self.duration, 0.0, 1.0)
        code = int(round(self.start_code + (self.end_code - self.start_code) * k))
        done = (k >= 1.0)
        if done:
            self.active = False
        return code, done

# -------------------- Main system --------------------
class GateSystem:
    def __init__(self, log_cb):
        self.log = log_cb

        self.state = "IDLE"       # IDLE / OPENING / CLOSING / STOPPED
        self.direction = 0        # +1 open, -1 close, 0 none
        self.position = 0.0       # 0 closed .. 1 open

        self.uz1 = False
        self.uz2 = False
        self.stress = 0.0

        self.ko = False
        self.kz = False

        self.kv_o1 = False
        self.kv_o2 = False
        self.kv_z1 = True
        self.kv_z2 = True

        self.slow_mode_30 = False  # авария №2 active (30%)

        self.dac_code = 0
        self.target_code = 0

        self.ramp = Ramp()

        self.sample_timer = 0.0

        # pulses for port display (one-sample tick)
        self.zp_adc_pulse = False
        self.zp_dac_pulse = False
        self.gt_flag = True  # in model we consider ADC ready after sampling

    def reset_sensors(self):
        self.uz1 = False
        self.uz2 = False
        self.stress = 0.0
        self.log("Сброс датчиков: УЗ1=0 УЗ2=0 усилие=0")

    def press_open(self):
        self.ko = True
        self.kz = False
        if self.state in ("IDLE", "STOPPED"):
            self.state = "OPENING"
            self.direction = +1
            self.slow_mode_30 = False
            self.log("Кнопка ОТКРЫТИЯ: старт OPENING, разгон 0→116В за 12с")
            self.start_ramp(self.nominal_target_code(), RAMP_UP_SEC)

    def press_close(self):
        self.kz = True
        self.ko = False
        if self.state in ("IDLE", "STOPPED"):
            self.state = "CLOSING"
            self.direction = -1
            self.slow_mode_30 = False
            self.log("Кнопка ЗАКРЫТИЯ: старт CLOSING, разгон 0→116В за 12с")
            self.start_ramp(self.nominal_target_code(), RAMP_UP_SEC)

    def start_ramp(self, target_code, duration):
        target_code = int(clamp(target_code, 0, 255))
        if target_code == self.target_code and self.ramp.active:
            return
        self.target_code = target_code
        self.ramp.start(self.dac_code, self.target_code, duration)
        self.zp_dac_pulse = True

    def nominal_target_code(self):
        # Номинальная цель зависит от концевика №1: если он уже сработал -> 50%, иначе 116%
        if self.state == "OPENING":
            return CODE_HALF if self.kv_o1 else CODE_NOM
        if self.state == "CLOSING":
            return CODE_HALF if self.kv_z1 else CODE_NOM
        return CODE_ZERO

    def update_limits_from_position(self):
        # limits computed from position
        self.kv_o1 = (self.position >= OPEN1_POS)
        self.kv_o2 = (self.position >= OPEN2_POS)

        self.kv_z1 = (self.position <= CLOSE1_POS)
        self.kv_z2 = (self.position <= CLOSE2_POS)

    def build_port300(self, adc_code):
        us_or = self.uz1 or self.uz2
        v = 0
        v |= (adc_code & 0xFF)
        v |= (1 if self.kv_o1 else 0) << 8
        v |= (1 if self.kv_o2 else 0) << 9
        v |= (1 if self.kv_z1 else 0) << 10
        v |= (1 if self.kv_z2 else 0) << 11
        v |= (1 if us_or else 0) << 12
        v |= (1 if self.ko else 0) << 13
        v |= (1 if self.kz else 0) << 14
        v |= (1 if self.gt_flag else 0) << 15
        return v

    def build_port301(self):
        v = 0
        v |= (self.dac_code & 0xFF)
        v |= (1 if self.zp_dac_pulse else 0) << 14
        v |= (1 if self.zp_adc_pulse else 0) << 15
        return v

    def emergency_stop(self, reason):
        if self.state != "STOPPED":
            self.log(f"АВАРИЯ: {reason} → останов по рис.Б.8б (5с до 0)")
        self.state = "STOPPED"
        self.direction = 0
        self.slow_mode_30 = False
        self.start_ramp(CODE_ZERO, RAMP_DOWN_SEC)

    def normal_stop_to_zero(self, reason):
        self.log(f"{reason} → по рис.Б.8б (5с до 0)")
        self.direction = 0
        self.slow_mode_30 = False
        self.start_ramp(CODE_ZERO, RAMP_DOWN_SEC)

    def control_step(self):
        # This step mimics "poll + analysis + output"
        self.zp_adc_pulse = True
        self.gt_flag = True

        stress_u = tenso_to_voltage(self.stress)
        adc_code = adc_code_from_voltage(stress_u)

        us_or = self.uz1 or self.uz2

        # Only active in motion states
        if self.state in ("OPENING", "CLOSING"):
            # авария №1: любой УЗ
            if us_or:
                self.emergency_stop("УЗ: обнаружено препятствие (УЗ1/УЗ2)")
                return adc_code

            # авария №3: 95% предела упругости
            if self.stress >= TH_95:
                self.emergency_stop("Тензо ≥ 95 кгс/мм²")
                return adc_code

            # авария №2: предел пропорциональности
            if self.stress >= TH_70:
                if not self.slow_mode_30:
                    self.slow_mode_30 = True
                    self.log("Тензо ≥ 70 → замедление до 30% по рис.Б.8б (5с)")
                    self.start_ramp(CODE_SLOW, RAMP_DOWN_SEC)
                # если в 30% — держим его (не вмешиваемся в КВ1)
                return adc_code

            # выход из аварии №2 (обратимо)
            if self.slow_mode_30 and self.stress < TH_55:
                self.slow_mode_30 = False
                nominal = self.nominal_target_code()
                self.log("Тензо < 55 → восстановить номинальную скорость по рис.Б.8а (12с)")
                self.start_ramp(nominal, RAMP_UP_SEC)
                return adc_code

            # концевики (замедления по рис.Б.8б)
            if self.state == "OPENING":
                if self.kv_o2:
                    self.normal_stop_to_zero("КВ_О2 сработал: конец открытия")
                    return adc_code
                if self.kv_o1 and not self.slow_mode_30:
                    # if not already targeting half or below
                    if self.target_code > CODE_HALF:
                        self.log("КВ_О1 сработал: замедление до 50% по рис.Б.8б (5с)")
                        self.start_ramp(CODE_HALF, RAMP_DOWN_SEC)
                        return adc_code

            if self.state == "CLOSING":
                if self.kv_z2:
                    self.normal_stop_to_zero("КВ_З2 сработал: конец закрытия")
                    return adc_code
                if self.kv_z1 and not self.slow_mode_30:
                    if self.target_code > CODE_HALF:
                        self.log("КВ_З1 сработал: замедление до 50% по рис.Б.8б (5с)")
                        self.start_ramp(CODE_HALF, RAMP_DOWN_SEC)
                        return adc_code

        return adc_code

    def update(self, dt):
        # reset pulses each frame; set during control_step
        self.zp_adc_pulse = False
        self.zp_dac_pulse = False

        # Update ramp continuously
        if self.ramp.active:
            self.dac_code, _ = self.ramp.update(dt)
        else:
            self.dac_code = int(self.target_code)

        # Move gate according to current voltage and direction (only when OPENING/CLOSING)
        u_out = dac_voltage_from_code(self.dac_code)
        if self.state in ("OPENING", "CLOSING") and self.direction != 0:
            # speed proportional to u_out / 116V
            frac = 0.0 if U_NOM <= 1e-6 else (u_out / U_NOM)
            frac = clamp(frac, 0.0, 1.2)
            v = BASE_SPEED * frac
            self.position += self.direction * v * dt
            self.position = clamp(self.position, 0.0, 1.0)

        # Update limit switches from position
        self.update_limits_from_position()

        # Sampling / decision step each 0.25s
        self.sample_timer += dt
        adc_code = 0
        while self.sample_timer >= SAMPLE_PERIOD:
            self.sample_timer -= SAMPLE_PERIOD
            adc_code = self.control_step()

        # If stopped by normal completion and ramp ended at 0 -> go IDLE
        if self.state != "STOPPED":
            if self.direction == 0 and self.target_code == 0 and not self.ramp.active:
                self.state = "IDLE"
                self.ko = False
                self.kz = False

        # If STOPPED and voltage already 0 -> just wait for operator (O/C)
        if self.state == "STOPPED" and self.target_code == 0 and not self.ramp.active:
            self.ko = False
            self.kz = False

        # Build ports for UI
        stress_u = tenso_to_voltage(self.stress)
        adc_code_now = adc_code_from_voltage(stress_u)
        port300 = self.build_port300(adc_code_now)
        port301 = self.build_port301()
        return port300, port301, adc_code_now, u_out, stress_u, (self.uz1 or self.uz2)

# -------------------- Drawing --------------------
def draw_rect(surf, rect, color, w=1):
    pygame.draw.rect(surf, color, rect, w)

def draw_text(surf, font, x, y, text, color=(230,230,230)):
    img = font.render(text, True, color)
    surf.blit(img, (x, y))
    return img.get_height()

def draw_bar(surf, rect, value01, label, color=(80,200,120), back=(50,50,50)):
    pygame.draw.rect(surf, back, rect, 0)
    w = int(rect.width * clamp(value01, 0.0, 1.0))
    pygame.draw.rect(surf, color, pygame.Rect(rect.x, rect.y, w, rect.height), 0)
    pygame.draw.rect(surf, (120,120,120), rect, 1)
    # label
    return

def draw_graph(surf, rect, series, t_now, seconds=60.0):
    # axes
    pygame.draw.rect(surf, (60,60,60), rect, 1)
    # grid
    for i in range(1, 5):
        y = rect.y + int(rect.height * i / 5)
        pygame.draw.line(surf, (35,35,35), (rect.x, y), (rect.x + rect.width, y), 1)
    for i in range(1, 6):
        x = rect.x + int(rect.width * i / 6)
        pygame.draw.line(surf, (35,35,35), (x, rect.y), (x, rect.y + rect.height), 1)

    # plot last "seconds"
    t0 = t_now - seconds
    pts = []
    for (t, u) in series:
        if t < t0:
            continue
        x = rect.x + int((t - t0) / seconds * rect.width)
        y = rect.y + rect.height - int(clamp(u / DAC_VMAX, 0.0, 1.0) * rect.height)
        pts.append((x, y))
    if len(pts) >= 2:
        pygame.draw.lines(surf, (120,220,255), False, pts, 2)

# -------------------- Main --------------------
def main():
    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Имитационная модель СРВ: автоматические ворота (вариант 30)")
    clock = pygame.time.Clock()

    font = pygame.font.SysFont("consolas", 18)
    font_small = pygame.font.SysFont("consolas", 16)
    font_big = pygame.font.SysFont("consolas", 28)

    log_lines = deque(maxlen=MAX_LOG_LINES)

    def log(msg):
        t = pygame.time.get_ticks() / 1000.0
        mm = int(t // 60)
        ss = int(t % 60)
        log_lines.appendleft(f"[{mm:02d}:{ss:02d}] {msg}")

    sysm = GateSystem(log)
    log("Готово. O=открыть, C=закрыть, 1/2=УЗ, ↑/↓=усилие, R=сброс")

    history = deque(maxlen=5000)

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        t_now = pygame.time.get_ticks() / 1000.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False

                if event.key == pygame.K_o:
                    sysm.press_open()
                if event.key == pygame.K_c:
                    sysm.press_close()

                if event.key == pygame.K_1:
                    sysm.uz1 = not sysm.uz1
                    log(f"УЗ1 = {fmt_bool(sysm.uz1)}")
                if event.key == pygame.K_2:
                    sysm.uz2 = not sysm.uz2
                    log(f"УЗ2 = {fmt_bool(sysm.uz2)}")

                if event.key == pygame.K_r:
                    sysm.reset_sensors()

        keys = pygame.key.get_pressed()
        if keys[pygame.K_UP]:
            sysm.stress = clamp(sysm.stress + 25.0 * dt, 0.0, TENSO_MAX)
        if keys[pygame.K_DOWN]:
            sysm.stress = clamp(sysm.stress - 25.0 * dt, 0.0, TENSO_MAX)

        port300, port301, adc_code, u_out, stress_u, us_or = sysm.update(dt)

        # history for graph
        history.append((t_now, u_out))

        # -------------------- Layout --------------------
        screen.fill((18, 18, 22))

        # panels
        left = pygame.Rect(20, 20, 520, 860)
        mid = pygame.Rect(560, 20, 720, 860)
        right = pygame.Rect(1300, 20, 280, 860)

        draw_rect(screen, left, (70,70,70), 1)
        draw_rect(screen, mid, (70,70,70), 1)
        draw_rect(screen, right, (70,70,70), 1)

        # Title
        draw_text(screen, font_big, 30, 30, "УПРАВЛЕНИЕ ВОРОТАМИ — ВАРИАНТ 30", (200,230,255))

        # -------- Left panel: sensors / ports --------
        y = 80
        draw_text(screen, font, 40, y, "ДАТЧИКИ / СИГНАЛЫ (порт 300h)", (180,220,180)); y += 30

        draw_text(screen, font, 40, y, f"KO (кнопка ОТКР): {fmt_bool(sysm.ko)}   (O)", (220,220,220)); y += 24
        draw_text(screen, font, 40, y, f"KZ (кнопка ЗАКР): {fmt_bool(sysm.kz)}   (C)", (220,220,220)); y += 24
        y += 6
        draw_text(screen, font, 40, y, f"УЗ1: {fmt_bool(sysm.uz1)} (1)   УЗ2: {fmt_bool(sysm.uz2)} (2)   US(OR): {fmt_bool(us_or)}", (220,180,180)); y += 24
        y += 6

        draw_text(screen, font, 40, y, f"КВ_О1: {fmt_bool(sysm.kv_o1)}   КВ_О2: {fmt_bool(sysm.kv_o2)}", (220,220,220)); y += 24
        draw_text(screen, font, 40, y, f"КВ_З1: {fmt_bool(sysm.kv_z1)}   КВ_З2: {fmt_bool(sysm.kv_z2)}", (220,220,220)); y += 24
        y += 10

        draw_text(screen, font, 40, y, f"Тензо: {sysm.stress:6.1f} кгс/мм²   Uдатч={stress_u:5.2f}В   АЦП={adc_code:3d} (0x{adc_code:02X})", (220,220,220)); y += 24

        # Stress bar
        bar = pygame.Rect(40, y+10, 460, 18)
        draw_bar(screen, bar, sysm.stress / TENSO_MAX, "stress")
        draw_text(screen, font_small, 40, y+32, "↑/↓ — изменить усилие", (160,160,160))
        y += 70

        draw_text(screen, font, 40, y, f"Порт 300h (вход):  0x{port300:04X}", (180,200,255)); y += 26
        draw_text(screen, font, 40, y, "Биты: 15 GT | 14 KZ | 13 KO | 12 US | 11 З2 | 10 З1 | 9 О2 | 8 О1 | 7..0 АЦП", (140,140,140)); y += 40

        draw_text(screen, font, 40, y, f"Порт 301h (выход): 0x{port301:04X}", (180,200,255)); y += 26
        draw_text(screen, font, 40, y, "Биты: 15 ZP_АЦП | 14 ZP_ЦАП | 7..0 Данные ЦАП", (140,140,140)); y += 40

        draw_text(screen, font, 40, y, f"ЦАП код: {sysm.dac_code:3d} (0x{sysm.dac_code:02X})  -> Uвых={u_out:6.1f} В", (220,220,220)); y += 24

        # Position bar
        draw_text(screen, font, 40, y+20, f"Положение створок: {sysm.position*100:5.1f}%", (220,220,220))
        bar2 = pygame.Rect(40, y+50, 460, 18)
        draw_bar(screen, bar2, sysm.position, "pos", color=(200,180,80))
        y += 90

        # -------- Mid panel: graph + log --------
        draw_text(screen, font, 580, 40, "ИСТОРИЯ НАПРЯЖЕНИЯ НА ЭЛЕКТРОПРИВОДЕ (0..120В)", (200,230,255))
        graph = pygame.Rect(580, 80, 680, 360)
        draw_graph(screen, graph, history, t_now, seconds=60.0)

        # Mark reference lines (116, 58, 34.8)
        for u, label in [(U_NOM, "116В"), (U_HALF, "58В"), (U_SLOW, "34.8В")]:
            yline = graph.y + graph.height - int(clamp(u / DAC_VMAX, 0.0, 1.0) * graph.height)
            pygame.draw.line(screen, (60,60,60), (graph.x, yline), (graph.x + graph.width, yline), 1)
            draw_text(screen, font_small, graph.x + graph.width + 10, yline - 8, label, (140,140,140))

        # Event log area
        draw_text(screen, font, 580, 470, "ЖУРНАЛ СОБЫТИЙ (последние строки, без скролла)", (200,230,255))
        log_rect = pygame.Rect(580, 500, 680, 360)
        pygame.draw.rect(screen, (30,30,34), log_rect, 0)
        pygame.draw.rect(screen, (60,60,60), log_rect, 1)

        yy = log_rect.y + 10
        for line in list(log_lines):
            draw_text(screen, font_small, log_rect.x + 10, yy, line, (220,220,220))
            yy += 24

        # -------- Right panel: current mode --------
        draw_text(screen, font, 1320, 40, "ТЕКУЩИЙ РЕЖИМ", (200,230,255))
        mode_box = pygame.Rect(1320, 80, 240, 120)
        pygame.draw.rect(screen, (28,28,32), mode_box, 0)
        pygame.draw.rect(screen, (80,80,80), mode_box, 1)

        mode_color = (120,220,255)
        if sysm.state == "STOPPED":
            mode_color = (255,110,110)
        elif sysm.state == "OPENING":
            mode_color = (120,255,160)
        elif sysm.state == "CLOSING":
            mode_color = (255,200,120)

        draw_text(screen, font_big, 1340, 110, sysm.state, mode_color)
        draw_text(screen, font_small, 1340, 150, f"slow30: {fmt_bool(sysm.slow_mode_30)}", (220,220,220))

        # Show what the controller tries to do
        draw_text(screen, font, 1320, 230, "ЦЕЛЕВОЙ УРОВЕНЬ", (200,230,255))
        target_u = dac_voltage_from_code(sysm.target_code)
        draw_text(screen, font, 1320, 260, f"target: {sysm.target_code:3d} (0x{sysm.target_code:02X})", (220,220,220))
        draw_text(screen, font, 1320, 286, f"Utarget: {target_u:6.1f} В", (220,220,220))

        draw_text(screen, font, 1320, 340, "ПРАВИЛА (кратко)", (200,230,255))
        rules = [
            "O/C: старт, разгон 12с",
            "КВ1: до 50% за 5с",
            "КВ2: до 0 за 5с",
            "УЗ или тензо>=95: стоп",
            "тензо>=70: до 30% (5с)",
            "тензо<55: возврат (12с)"
        ]
        yy = 370
        for r in rules:
            draw_text(screen, font_small, 1320, yy, "- " + r, (190,190,190))
            yy += 22

        draw_text(screen, font_small, 1320, 850, "O/C/1/2/↑/↓/R, Esc", (140,140,140))

        pygame.display.flip()

    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
