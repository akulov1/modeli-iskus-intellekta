import sys
import pygame
from collections import deque

ADC_BITS = 8
DAC_BITS = 8

ADC_U_MIN = 0.0
ADC_U_MAX = 19.5

DAC_U_MIN = 0.0
DAC_U_MAX = 120.0

U_NOM = 116.0  # nominal drive voltage

T_RAMP_UP = 12.0   #0 -> 116 in 12 s
T_RAMP_DOWN = 5.0  # 116 -> 0 in 5 s

# Gate travel model
POS_MIN = 0.0     # 0% = fully closed
POS_MAX = 100.0   # 100% = fully open

OPEN_SPEED_AT_NOM = (POS_MAX - POS_MIN) / 12.0
CLOSE_SPEED_AT_NOM = (POS_MAX - POS_MIN) / 5.0

KV_OPEN1_POS = 90.0
KV_OPEN2_POS = 100.0
KV_CLOSE1_POS = 10.0
KV_CLOSE2_POS = 0.0

# аварийные пороги по усилию
F_SLOW_ON = 70.0
F_SLOW_OFF = 55.0
F_STOP = 114.0

TEN_F_MIN = 0.0
TEN_F_MAX = 120.0
TEN_U_MIN = 0.0
TEN_U_MAX = 15.0

SLOW_KV_FACTOR = 0.5
SLOW_FORCE_FACTOR = 0.30

def clamp(x, a, b):
    return max(a, min(b, x))


def adc_code_from_voltage(u):
    u = clamp(u, ADC_U_MIN, ADC_U_MAX)
    code = int(round((u - ADC_U_MIN) * ((2 ** ADC_BITS - 1) / (ADC_U_MAX - ADC_U_MIN))))
    return clamp(code, 0, 2 ** ADC_BITS - 1)


def dac_code_from_voltage(u):
    u = clamp(u, DAC_U_MIN, DAC_U_MAX)
    code = int(round((u - DAC_U_MIN) * ((2 ** DAC_BITS - 1) / (DAC_U_MAX - DAC_U_MIN))))
    return clamp(code, 0, 2 ** DAC_BITS - 1)


def ten_voltage_from_force(f):
    f = clamp(f, TEN_F_MIN, TEN_F_MAX)
    return TEN_U_MIN + (f - TEN_F_MIN) * (TEN_U_MAX - TEN_U_MIN) / (TEN_F_MAX - TEN_F_MIN)


def hex16(x):
    return f"0x{x & 0xFFFF:04X}"


def hex8(x):
    return f"0x{x & 0xFF:02X}"


def wrap_lines(s, font, max_w):
    words = s.split(" ")
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class LinearProfile:
    def __init__(self, u0, u1, duration):
        self.u0 = float(u0)
        self.u1 = float(u1)
        self.duration = max(0.001, float(duration))
        self.t = 0.0
        self.done = False

    def step(self, dt):
        if self.done:
            return self.u1
        self.t += dt
        k = clamp(self.t / self.duration, 0.0, 1.0)
        u = self.u0 + (self.u1 - self.u0) * k
        if k >= 1.0:
            self.done = True
        return u


class GateController:
    def __init__(self):
        self.state = "IDLE"  # IDLE / OPENING / CLOSING / STOPPED
        self.direction = 0   # 0 close, 1 open
        self.u_cmd = 0.0

        self.slow_force = False
        self.slow_kv = False

        self.profile = None
        self.stop_latched = False  # for авария 1/3: require operator restart

        self.adc_start_pulse = 0.0
        self.dac_start_pulse = 0.0

        self.log = deque(maxlen=18)
        self.runtime = 0.0

    def push_log(self, s):
        self.log.appendleft(s)

    def set_profile(self, target, duration):
        self.profile = LinearProfile(self.u_cmd, target, duration)

    def request_open(self):
        self.stop_latched = False
        self.state = "OPENING"
        self.direction = 1
        self.slow_kv = False
        self.set_profile(U_NOM, T_RAMP_UP)
        self.push_log("Кнопка ОТКРЫТЬ: запуск (разгон по Б.8а)")

    def request_close(self):
        self.stop_latched = False
        self.state = "CLOSING"
        self.direction = 0
        self.slow_kv = False
        self.set_profile(U_NOM, T_RAMP_UP)
        self.push_log("Кнопка ЗАКРЫТЬ: запуск (разгон по Б.8а)")

    def emergency_stop(self, reason):
        if not self.stop_latched:
            self.push_log(f"СТОП: {reason} (перезапуск только кнопкой)")
        self.stop_latched = True
        self.state = "STOPPED"
        self.slow_force = False
        self.slow_kv = False
        self.set_profile(0.0, T_RAMP_DOWN)

    def apply_slow_force(self):
        if not self.slow_force:
            self.slow_force = True
            self.push_log("АС2: усилие >= 70 -> замедление до 30% (Б.8б)")
        self.set_profile(U_NOM * SLOW_FORCE_FACTOR, T_RAMP_DOWN)

    def release_slow_force(self):
        if self.slow_force:
            self.slow_force = False
            self.push_log("АС2: усилие < 55 -> восстановление скорости (Б.8а)")
        self.set_profile(U_NOM, T_RAMP_UP)

    def apply_slow_kv(self):
        if not self.slow_kv:
            self.slow_kv = True
            self.push_log("КВ1: замедление до 50% (Б.8б)")
        self.set_profile(U_NOM * SLOW_KV_FACTOR, T_RAMP_DOWN)

    def step(self, dt, sensors):
        self.runtime += dt

        self.adc_start_pulse = 0.02
        adc_ready = True

        moving = self.state in ("OPENING", "CLOSING")

        if moving and (sensors["us1"] or sensors["us2"]):
            self.emergency_stop("АС1: препятствие (УЗ)")

        if moving:
            f = sensors["force"]
            if f >= F_STOP:
                self.emergency_stop("АС3: усилие >= 114")
            elif f >= F_SLOW_ON:
                self.apply_slow_force()
            elif f < F_SLOW_OFF:
                if self.slow_force:
                    self.release_slow_force()

        if moving:
            if self.state == "OPENING":
                if sensors["kv_open2"]:
                    self.push_log("КВ2 ОТКРЫТО: останов")
                    self.state = "IDLE"
                    self.set_profile(0.0, T_RAMP_DOWN)
                elif sensors["kv_open1"]:
                    self.apply_slow_kv()

            elif self.state == "CLOSING":
                if sensors["kv_close2"]:
                    self.push_log("КВ2 ЗАКРЫТО: останов")
                    self.state = "IDLE"
                    self.set_profile(0.0, T_RAMP_DOWN)
                elif sensors["kv_close1"]:
                    self.apply_slow_kv()

        if self.profile is not None:
            self.u_cmd = self.profile.step(dt)
            if self.profile.done:
                self.profile = None

        self.dac_start_pulse = 0.02
        return adc_ready

class World:
    def __init__(self):
        self.pos = 0.0
        self.force = 0.0
        self.us1 = False
        self.us2 = False

    def compute_limit_switches(self):
        kv_open1 = self.pos >= KV_OPEN1_POS
        kv_open2 = self.pos >= KV_OPEN2_POS
        kv_close1 = self.pos <= KV_CLOSE1_POS
        kv_close2 = self.pos <= KV_CLOSE2_POS
        return kv_open1, kv_open2, kv_close1, kv_close2

    def step(self, dt, ctrl: GateController):
        u = clamp(ctrl.u_cmd, 0.0, U_NOM)

        if ctrl.state in ("OPENING", "CLOSING") and not ctrl.stop_latched:
            if ctrl.direction == 1:
                v = OPEN_SPEED_AT_NOM * (u / U_NOM)
                self.pos += v * dt
            else:
                v = CLOSE_SPEED_AT_NOM * (u / U_NOM)
                self.pos -= v * dt

        self.pos = clamp(self.pos, POS_MIN, POS_MAX)

    def sensors_snapshot(self):
        kv_open1, kv_open2, kv_close1, kv_close2 = self.compute_limit_switches()
        return {
            "pos": self.pos,
            "force": self.force,
            "us1": self.us1,
            "us2": self.us2,
            "kv_open1": kv_open1,
            "kv_open2": kv_open2,
            "kv_close1": kv_close1,
            "kv_close2": kv_close2,
        }


pygame.init()
W, H = 1400, 820
screen = pygame.display.set_mode((W, H))
pygame.display.set_caption("ИМИТАЦИОННАЯ МОДЕЛЬ: УПРАВЛЕНИЕ АВТОМАТИЧЕСКИМИ ВОРОТАМИ (Вариант 30)")
clock = pygame.time.Clock()

FONT = pygame.font.SysFont("consolas", 16)
FONT_B = pygame.font.SysFont("consolas", 18, bold=True)
FONT_S = pygame.font.SysFont("consolas", 14)


def text(s, x, y, color=(220, 220, 220), bold=False):
    surf = (FONT_B if bold else FONT).render(s, True, color)
    screen.blit(surf, (x, y))


def text_s(s, x, y, color=(220, 220, 220)):
    surf = FONT_S.render(s, True, color)
    screen.blit(surf, (x, y))


def draw_panel(rect, title):
    pygame.draw.rect(screen, (30, 30, 30), rect, border_radius=8)
    pygame.draw.rect(screen, (80, 80, 80), rect, 1, border_radius=8)
    text(title, rect.x + 10, rect.y + 8, (0, 200, 160), bold=True)


def draw_bar(x, y, w, h, frac, label, color=(0, 220, 120)):
    pygame.draw.rect(screen, (55, 55, 55), (x, y, w, h))
    pygame.draw.rect(screen, (120, 120, 120), (x, y, w, h), 1)
    fill = int(w * clamp(frac, 0.0, 1.0))
    pygame.draw.rect(screen, color, (x, y, fill, h))
    text(label, x, y - 18, (180, 180, 180))


def draw_led(x, y, on, label, on_col=(255, 60, 60), off_col=(90, 90, 90)):
    col = on_col if on else off_col
    pygame.draw.circle(screen, col, (x, y), 8)
    pygame.draw.circle(screen, (180, 180, 180), (x, y), 8, 1)
    text(label, x + 14, y - 8, (220, 220, 220))


def draw_graph(rect, values, vmin, vmax, color=(0, 220, 120), label=""):
    pygame.draw.rect(screen, (15, 15, 15), rect)
    pygame.draw.rect(screen, (100, 100, 100), rect, 1)
    if label:
        text(label, rect.x + 8, rect.y + 6, (180, 180, 180))
    if len(values) < 2:
        return
    pts = []
    for i, v in enumerate(values):
        x = rect.x + int(i * (rect.w - 2) / (len(values) - 1)) + 1
        frac = (v - vmin) / (vmax - vmin) if vmax > vmin else 0.0
        frac = clamp(frac, 0.0, 1.0)
        y = rect.y + rect.h - int(frac * (rect.h - 2)) - 1
        pts.append((x, y))
    if len(pts) >= 2:
        pygame.draw.lines(screen, color, False, pts, 2)


world = World()
ctrl = GateController()

hist_len = 380
hist_u = deque([0.0] * hist_len, maxlen=hist_len)
hist_pos = deque([0.0] * hist_len, maxlen=hist_len)
hist_force = deque([0.0] * hist_len, maxlen=hist_len)

running = True
while running:
    dt = clock.tick(60) / 1000.0

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                running = False

            if event.key == pygame.K_o:
                ctrl.request_open()
            if event.key == pygame.K_c:
                ctrl.request_close()

            if event.key == pygame.K_1:
                world.us1 = not world.us1
                ctrl.push_log(f"УЗ1: {'ПОМЕХА' if world.us1 else 'норма'}")
            if event.key == pygame.K_2:
                world.us2 = not world.us2
                ctrl.push_log(f"УЗ2: {'ПОМЕХА' if world.us2 else 'норма'}")

            if event.key == pygame.K_r:
                world.us1 = False
                world.us2 = False
                world.force = 0.0
                ctrl.push_log("Сброс: УЗ=норма, усилие=0")

            if event.key == pygame.K_UP:
                world.force = clamp(world.force + 2.0, 0.0, 120.0)
            if event.key == pygame.K_DOWN:
                world.force = clamp(world.force - 2.0, 0.0, 120.0)

    ctrl.adc_start_pulse = max(0.0, ctrl.adc_start_pulse - dt)
    ctrl.dac_start_pulse = max(0.0, ctrl.dac_start_pulse - dt)

    sensors = world.sensors_snapshot()
    adc_ready = ctrl.step(dt, sensors)
    world.step(dt, ctrl)

    ten_u = ten_voltage_from_force(world.force)
    adc_code = adc_code_from_voltage(ten_u)

    dac_u = clamp(ctrl.u_cmd, 0.0, DAC_U_MAX)
    dac_code = dac_code_from_voltage(dac_u)

    sensors = world.sensors_snapshot()

    hist_u.append(dac_u)
    hist_pos.append(sensors["pos"])
    hist_force.append(world.force)

    in_port = 0
    in_port |= (adc_code & 0xFF)
    in_port |= (1 << 8) if sensors["kv_open1"] else 0
    in_port |= (1 << 9) if sensors["kv_open2"] else 0
    in_port |= (1 << 10) if sensors["kv_close1"] else 0
    in_port |= (1 << 11) if sensors["kv_close2"] else 0
    in_port |= (1 << 12) if world.us1 else 0
    in_port |= (1 << 13) if world.us2 else 0
    in_port |= (1 << 15) if adc_ready else 0

    out_port = 0
    out_port |= (dac_code & 0xFF)
    out_port |= (1 << 8) if ctrl.direction == 1 else 0
    out_port |= (1 << 14) if ctrl.dac_start_pulse > 0 else 0
    out_port |= (1 << 15) if ctrl.adc_start_pulse > 0 else 0

    screen.fill((0, 0, 0))

    text("УПРАВЛЕНИЕ ВОРОТАМИ (вариант 30) | O=Открыть  C=Закрыть  1/2=УЗ  ↑↓=усилие  R=сброс  Esc=выход",
         20, 12, (200, 200, 200))

    left = pygame.Rect(20, 50, 420, 740)
    mid = pygame.Rect(460, 50, 660, 500)
    right = pygame.Rect(1140, 50, 240, 740)

    draw_panel(left, "ДАТЧИКИ / СОСТОЯНИЕ")
    draw_panel(mid, "ИСТОРИЯ / ГРАФИКИ")
    draw_panel(right, "ТЕКУЩИЙ РЕЖИМ / УПРАВЛЕНИЕ")

    y = left.y + 40
    text(f"Время работы: {ctrl.runtime:6.1f} c", left.x + 14, y); y += 22

    pos_frac = (sensors["pos"] - POS_MIN) / (POS_MAX - POS_MIN)
    draw_bar(left.x + 14, y + 10, left.w - 28, 16, pos_frac, f"Позиция створок: {sensors['pos']:5.1f}%")
    y += 46

    text("Концевые выключатели:", left.x + 14, y, (120, 200, 255)); y += 22
    draw_led(left.x + 24, y + 8, sensors["kv_open1"], "КВ Откр.1 (>=90%)"); y += 22
    draw_led(left.x + 24, y + 8, sensors["kv_open2"], "КВ Откр.2 (=100%)", on_col=(255, 170, 60)); y += 22
    draw_led(left.x + 24, y + 8, sensors["kv_close1"], "КВ Закр.1 (<=10%)"); y += 22
    draw_led(left.x + 24, y + 8, sensors["kv_close2"], "КВ Закр.2 (=0%)", on_col=(255, 170, 60)); y += 34

    text("Ультразвуковые датчики:", left.x + 14, y, (120, 200, 255)); y += 22
    draw_led(left.x + 24, y + 8, world.us1, "УЗ1: препятствие", on_col=(255, 60, 60), off_col=(0, 160, 80)); y += 22
    draw_led(left.x + 24, y + 8, world.us2, "УЗ2: препятствие", on_col=(255, 60, 60), off_col=(0, 160, 80)); y += 34

    text("Тензодатчик:", left.x + 14, y, (120, 200, 255)); y += 22
    text(f"Усилие: {world.force:6.1f} кгс/мм^2", left.x + 14, y); y += 20
    text(f"U(тенз): {ten_u:6.2f} V   (0..15V)", left.x + 14, y); y += 20
    text(f"АЦП (8 бит, 0..19.5V): {hex8(adc_code)} ({adc_code})", left.x + 14, y); y += 24

    text("Пороги (тензодатчик):", left.x + 14, y, (200, 180, 120)); y += 18
    text("  >=70  -> замедление 30%  (код ~114 / 0x72)", left.x + 14, y, (200, 180, 120)); y += 18
    text("  <55   -> восстановление (код ~90  / 0x5A)", left.x + 14, y, (200, 180, 120)); y += 18
    text("  >=114 -> аварийный стоп (код ~186 / 0xBA)", left.x + 14, y, (200, 180, 120)); y += 22

    text("СОСТОЯНИЕ ПОРТОВ УСРР:", left.x + 14, y, (0, 220, 180), bold=True); y += 24
    text(f"Порт 300h (вход):  {hex16(in_port)}", left.x + 14, y); y += 20
    text(" 0..7  АЦП | 8 Откр1 | 9 Откр2 | 10 Закр1 | 11 Закр2", left.x + 14, y); y += 18
    text(" 12 УЗ1 | 13 УЗ2 | 15 ГТ(АЦП)", left.x + 14, y); y += 22
    text(f"Порт 301h (выход): {hex16(out_port)}", left.x + 14, y); y += 20
    text(" 0..7 ЦАП | 8 Напр.(1=Откр) | 14 ЗП(ЦАП) | 15 ЗП(АЦП)", left.x + 14, y); y += 18

    gx = mid.x + 14
    gy = mid.y + 40
    g1 = pygame.Rect(gx, gy, mid.w - 28, 150)
    g2 = pygame.Rect(gx, gy + 170, mid.w - 28, 150)
    g3 = pygame.Rect(gx, gy + 340, mid.w - 28, 140)

    draw_graph(g1, list(hist_u), 0.0, 120.0, color=(0, 220, 120), label="Uупр (ЦАП), В (0..120)")
    draw_graph(g2, list(hist_pos), 0.0, 100.0, color=(120, 200, 255), label="Позиция ворот, % (0..100)")
    draw_graph(g3, list(hist_force), 0.0, 120.0, color=(255, 180, 60), label="Усилие, кгс/мм^2 (0..120)")

    ry = right.y + 40
    mode_col = (0, 200, 120)
    if ctrl.state == "OPENING":
        mode_col = (120, 200, 255)
    elif ctrl.state == "CLOSING":
        mode_col = (255, 200, 120)
    elif ctrl.state == "STOPPED":
        mode_col = (255, 60, 60)

    text("Состояние:", right.x + 14, ry, (180, 180, 180)); ry += 22
    text(ctrl.state, right.x + 14, ry, mode_col, bold=True); ry += 26
    text(f"Uупр: {dac_u:6.1f} В", right.x + 14, ry); ry += 18
    text(f"Код ЦАП: {hex8(dac_code)} ({dac_code})", right.x + 14, ry); ry += 18
    text(f"slow_force: {'ДА' if ctrl.slow_force else 'нет'}", right.x + 14, ry, (200, 180, 120)); ry += 18
    text(f"slow_kv:    {'ДА' if ctrl.slow_kv else 'нет'}", right.x + 14, ry, (200, 180, 120)); ry += 22

    card = pygame.Rect(right.x + 14, ry, right.w - 28, 110)
    pygame.draw.rect(screen, (25, 25, 25), card, border_radius=8)
    pygame.draw.rect(screen, mode_col, card, 2, border_radius=8)

    if ctrl.state == "IDLE":
        desc = ["Ожидание", "U=0, привод стоп"]
    elif ctrl.state == "OPENING":
        desc = ["Открытие", "Контроль УЗ/усилия/КВ"]
    elif ctrl.state == "CLOSING":
        desc = ["Закрытие", "Контроль УЗ/усилия/КВ"]
    else:
        desc = ["АВАРИЯ / STOP", "Перезапуск кнопкой"]

    text("РЕЖИМ", card.x + 10, card.y + 10, mode_col, bold=True)
    for i, line in enumerate(desc):
        text(line, card.x + 10, card.y + 40 + i * 18, (220, 220, 220))

    ry += 130
    text("Журнал событий:", right.x + 14, ry, (180, 180, 180)); ry += 22

    log_box = pygame.Rect(right.x + 14, ry, right.w - 28, right.h - (ry - right.y) - 14)
    pygame.draw.rect(screen, (15, 15, 15), log_box, border_radius=8)
    pygame.draw.rect(screen, (80, 80, 80), log_box, 1, border_radius=8)

    max_text_w = log_box.w - 20
    ly = log_box.y + 10

    for s in list(ctrl.log):
        for line in wrap_lines(s, FONT_S, max_text_w):
            if ly > log_box.y + log_box.h - 18:
                break
            text_s(line, log_box.x + 10, ly, (200, 200, 200))
            ly += 16
        if ly > log_box.y + log_box.h - 18:
            break

    pygame.display.flip()

pygame.quit()
sys.exit()
