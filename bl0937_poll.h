#pragma once
//
// bl0937_poll.h — измеритель U/I/P (BL0937/HLW8012) ОПРОСОМ, без прерываний.
//
// Почему опросом: штатный компонент hlw8012 на LibreTiny не считает импульсы
// (прерывания на rtl87xx не работают) -> «везде нули». Измеритель опрашивается
// в C++: CF1 при SEL=1 даёт ~1684 Гц ≈ 224 В.
//
// Показания стоит сверить с мультиметром перед боевым применением.
//
// Пины (подтверждены на живом железе):
//   SEL = PA9  — 1 -> напряжение, 0 -> ток
//   CF1 = PA2  — частота U или I (в зависимости от SEL)
//   CF  = PA3  — частота, пропорциональная мощности
//
// Коэффициенты калибровки (значение = K * частота):
//   U[В]  = 0.133390392 * f
//   I[А]  = 0.010285907 * f
//   P[Вт] = 1.238737500 * f
//
#include <Arduino.h>
#include <math.h>

namespace bl0937 {

// ---------- конфигурация ----------
static const uint8_t PIN_SEL = 9;   // PA9
static const uint8_t PIN_CF1 = 2;   // PA2
static const uint8_t PIN_CF = 3;    // PA3

static const float K_U = 0.133390392f;
static const float K_I = 0.010285907f;
static const float K_P = 1.238737500f;

// Пауза после переключения SEL, чтобы измеритель перестроился, и окно счёта.
// Суммарная блокировка цикла: 2*(40+120) + 120 = ~440 мс.
static const uint32_t SETTLE_MS = 40;
static const uint32_t WINDOW_US = 120000;

// ---------- результаты (публикуются из interval в yaml) ----------
static float voltage = NAN;
static float current = NAN;
static float power = NAN;

// ---------- внутреннее ----------
static bool pins_ready = false;

static void init_pins() {
  if (pins_ready)
    return;
  pinMode(PIN_SEL, OUTPUT);
  pinMode(PIN_CF1, INPUT);
  pinMode(PIN_CF, INPUT);
  pins_ready = true;
}

// Частота по фронтам в окне. Считаем ПО ПЕРВОМУ И ПОСЛЕДНЕМУ фронту, а не
// «число фронтов / окно» — так не набегает ошибка от обрезанных краёв окна.
static float measure_freq(uint8_t pin, uint32_t window_us) {
  const uint32_t t_start = micros();
  int last = digitalRead(pin);
  uint32_t edges = 0;
  uint32_t t_first = 0, t_last = 0;

  while ((uint32_t)(micros() - t_start) < window_us) {
    int now = digitalRead(pin);
    if (now != last) {
      if (now == HIGH) {  // считаем только нарастающие фронты
        uint32_t t = micros();
        if (edges == 0)
          t_first = t;
        t_last = t;
        edges++;
      }
      last = now;
    }
  }

  if (edges < 2)
    return 0.0f;  // импульсов нет (или один) — считаем, что сигнала нет
  uint32_t span = t_last - t_first;
  if (span == 0)
    return 0.0f;
  return (float)(edges - 1) * 1000000.0f / (float)span;
}

// Один полный замер: U, затем I (переключая SEL), затем P.
static void measure() {
  init_pins();

  // --- напряжение: SEL = 1 ---
  digitalWrite(PIN_SEL, HIGH);
  delay(SETTLE_MS);
  float f_u = measure_freq(PIN_CF1, WINDOW_US);
  // 0 Гц на напряжении — аномалия (сеть всегда есть) -> NaN, чтобы не публиковать
  voltage = (f_u > 0.0f) ? (K_U * f_u) : NAN;

  yield();

  // --- ток: SEL = 0 ---
  digitalWrite(PIN_SEL, LOW);
  delay(SETTLE_MS);
  float f_i = measure_freq(PIN_CF1, WINDOW_US);
  current = K_I * f_i;  // 0 Гц = нет нагрузки, это законный ноль

  yield();

  // --- мощность: CF (от SEL не зависит) ---
  float f_p = measure_freq(PIN_CF, WINDOW_US);
  power = K_P * f_p;  // 0 Гц = нет нагрузки

  yield();
}

}  // namespace bl0937
