# Единый формат записи данных о здоровье и режиме

Контракт хранения, в который пишут все источники: генератор синтетики, а позже — адаптеры
Apple Health, WHOOP и кольца Сбера. Документ описывает **форму записи**, **словарь метрик** и
**требования к адаптерам**.

> **Дисклеймер.** Диапазоны и формулы здесь подобраны так, чтобы синтетические данные выглядели
> правдоподобно и годились для отладки аналитики. Это **не клинический референс** и не основание
> для каких-либо выводов о здоровье реального человека.

---

## 1. Форма: длинный формат, одна строка — одно наблюдение

Запись выглядит так:

```
subject_id, record_id, date, period, observed_at, tz, tz_offset_min,
metric, value, unit, source, source_device, method, method_detail, quality, missing_reason
```

**Почему длинный формат, а не широкая таблица «дата × показатели».** Три источника пишут разные
подмножества метрик с разной частотой: WHOOP даёт HRV каждую ночь, лаборатория — раз в месяц,
кольцо появляется в середине наблюдения. В широкой таблице это превращается в решето из пустых
колонок, а добавление четвёртого источника ломает схему. В длинном формате новый источник или
новая метрика — это просто новые строки.

Плата за это — нужно помнить, какие метрики бывают и в каких единицах. Отсюда словарь в разделе 3.

### Поля

| поле | тип | пример | зачем |
|---|---|---|---|
| `subject_id` | str | `synt-001` | несколько профилей в одном файле |
| `record_id` | str | `synt-001:2026-01-05:hrv_rmssd:whoop` | ключ идемпотентности: повторный импорт того же наблюдения перезаписывает строку, а не дублирует её |
| `date` | ISO date | `2026-01-05` | календарная дата, к которой отнесён показатель |
| `period` | enum | `night` | `night` — ночь **на** эту дату; `day` — сутки целиком; `point` — момент времени |
| `observed_at` | ISO 8601 с офсетом | `2026-01-05T07:12:00+03:00` | точный момент; обязателен для `point`, желателен для `night` |
| `tz` | IANA | `Europe/Moscow` | часовой пояс, в котором человек находился |
| `tz_offset_min` | int | `180` | офсет в минутах, чтобы читать файл без базы таймзон |
| `metric` | str | `hrv_rmssd` | канонический id из словаря (раздел 3) |
| `value` | float \| int \| null | `41.2` | `null` **только** вместе с заполненным `missing_reason` |
| `unit` | str | `ms` | всегда явно, даже если однозначно следует из метрики |
| `source` | enum | `whoop` | `apple_health` \| `whoop` \| `sber_ring` \| `lab` \| `manual` |
| `source_device` | str | `WHOOP 4.0` | конкретная модель: смена устройства и прошивки меняет алгоритм |
| `method` | enum | `measured` | `measured` \| `derived` \| `aggregated` \| `self_reported` \| `imputed` |
| `method_detail` | str | `rmssd_slow_wave_sleep_5min` | как именно посчитано (см. раздел 2.1) |
| `quality` | float 0..1 | `0.86` | покрытие/уверенность источника; `null`, если источник её не отдаёт |
| `missing_reason` | enum \| null | `battery` | `not_worn` \| `battery` \| `not_synced` \| `out_of_range` |

**Представление `null`.** В CSV — пустая строка, в JSONL — `null`. Строка `"null"`, `"NA"`, `-1`
и `0` как маркеры отсутствия **запрещены**: ноль шагов — это валидное наблюдение, а не пропуск.

---

## 2. Четыре решения, которые определяют формат

### 2.1. `method` + `method_detail` — условие сопоставимости, а не метаданные «на всякий случай»

Apple Health отдаёт вариабельность как **SDNN** по короткому дневному замеру. WHOOP отдаёт
**RMSSD**, усреднённый по фазе медленного сна. Это разные величины: разная математика, разное
время суток, разная физиология. Числа отличаются в разы, и «средняя вариабельность за неделю»,
посчитанная по смеси этих двух рядов, не значит ничего.

Поэтому:

- `hrv_rmssd` и `hrv_sdnn` — **разные метрики словаря**, а не одна метрика от разных источников;
- `method_detail` фиксирует окно и алгоритм строкой из фиксированного набора
  (`rmssd_slow_wave_sleep_5min`, `sdnn_spot_check_60s`, `rmssd_night_average` …);
- любая агрегация нескольких метрик обязана сначала проверить, что `method_detail` совпадает.

Практическое правило: **сравнивать значения можно только внутри одной пары
`(metric, method_detail)`.** Между парами сравнивают динамику, приведённую к персональной базе
(z-оценка, отклонение от медианы), но не абсолютные значения.

### 2.2. Пропуск — это строка, а не отсутствие строки

Есть два разных «нет данных», и путать их нельзя:

| ситуация | как выглядит в данных | как читать |
|---|---|---|
| устройство было, но не измерило | строка есть, `value=null`, `missing_reason` заполнен | пропуск; исключить из средних, но учесть в оценке покрытия |
| источника в этот период не существовало | строки нет вообще | не пропуск; ряд источника просто начинается позже |

Кольцо Сбера в синтетическом наборе появляется с 64-го дня — до этого дня строк с
`source=sber_ring` **нет**. Это не пропуски, и считать покрытие кольца от начала наблюдения нельзя.

### 2.3. Конфликт источников не разрешается на записи

Если в один день WHOOP и кольцо оба дали `resting_hr`, в файле лежат **обе строки**. Формат
хранит наблюдения, а не «правду». Приоритет — отдельный слой поверх хранения:

```
lab > whoop > sber_ring > apple_health > manual
```

Правило применяется на чтении и **должно быть отражено в отчёте аналитики**: «RHR взят из WHOOP,
показания кольца отброшены как дубль». Молча выбирать один источник при записи нельзя — это
необратимая потеря данных и невозможность потом сверить устройства между собой.

Отдельный частый случай: **два источника систематически расходятся**. Кольцо даёт HRV примерно
на 12% выше WHOOP просто потому, что считает по другому окну. При смене основного источника ряд
скачком меняет уровень, хотя с человеком ничего не произошло. Аналитика обязана детектировать
такой скачок по смене `source`/`source_device`, а не объяснять его физиологией.

### 2.4. Время суток хранится числом, а не строкой

`sleep_onset` и `sleep_end` — численные метрики: **минуты от локальной полуночи, отрицательные
значения = до полуночи**. Отбой в 23:40 → `-20`, в 01:15 → `75`.

Так время попадает в общую колонку `value` и участвует в арифметике (среднее, SD, корреляции)
без парсинга. Точный момент при этом не теряется — он лежит в `observed_at` вместе с офсетом.

Важно, что отсчёт идёт от **локальной** полуночи: в командировке человек может лечь в 23:00 по
местному времени, и это в данных выглядит как обычный отбой, хотя по «домашнему» времени это
середина дня. Смена режима видна через `tz`, а не через сдвиг `sleep_onset`.

---

## 3. Словарь метрик

`P` — период (`night` / `day` / `point`). Диапазон — правдоподобные границы, значения за ними
помечаются `missing_reason=out_of_range`.

### Сон

| metric | unit | P | диапазон | источники |
|---|---|---|---|---|
| `sleep_onset` | `min` | night | −240 … 300 | whoop, apple_health, sber_ring |
| `sleep_end` | `min` | night | 180 … 780 | whoop, apple_health, sber_ring |
| `sleep_duration_min` | `min` | night | 120 … 720 | whoop, apple_health, sber_ring |
| `sleep_efficiency_pct` | `%` | night | 55 … 100 | whoop, sber_ring |
| `deep_sleep_min` | `min` | night | 0 … 210 | whoop, sber_ring |
| `rem_sleep_min` | `min` | night | 0 … 240 | whoop, sber_ring |
| `awakenings` | `count` | night | 0 … 25 | whoop, sber_ring |

### Сердце и дыхание

| metric | unit | P | диапазон | источники |
|---|---|---|---|---|
| `hrv_rmssd` | `ms` | night | 8 … 180 | whoop, sber_ring |
| `hrv_sdnn` | `ms` | day | 10 … 200 | apple_health |
| `resting_hr` | `bpm` | night | 35 … 100 | whoop, sber_ring, apple_health |
| `respiratory_rate` | `brpm` | night | 9 … 24 | whoop, sber_ring |
| `spo2_pct` | `%` | night | 88 … 100 | whoop, sber_ring |
| `skin_temp_deviation_c` | `Cel` | night | −2.0 … 2.5 | whoop, sber_ring |

### Активность и нагрузка

| metric | unit | P | диапазон | источники |
|---|---|---|---|---|
| `steps` | `count` | day | 0 … 45000 | apple_health, sber_ring |
| `active_energy_kcal` | `kcal` | day | 0 … 3000 | apple_health, whoop |
| `workout_min` | `min` | day | 0 … 300 | whoop, apple_health |

### Производные и самоотчёт

| metric | unit | P | диапазон | источники | method |
|---|---|---|---|---|---|
| `readiness_score` | `score` | day | 0 … 100 | whoop, sber_ring | `derived` |
| `alcohol_units` | `U` | day | 0 … 20 | manual | `self_reported` |
| `body_mass_kg` | `kg` | point | 35 … 200 | manual, apple_health | `measured` |

### Лаборатория (`source=lab`, `period=point`, `method=measured`)

| metric | unit | диапазон | заметка |
|---|---|---|---|
| `ferritin` | `ng/mL` | 3 … 500 | реактант острой фазы: растёт при воспалении независимо от запасов железа |
| `vitamin_d_25oh` | `ng/mL` | 5 … 100 | выраженная сезонность |
| `hs_crp` | `mg/L` | 0.1 … 60 | маркер воспаления; читать вместе с ферритином |
| `hba1c` | `%` | 4.0 … 9.0 | инерция ~3 месяца, на недельные события не реагирует |
| `tsh` | `mIU/L` | 0.2 … 10 | контрольный аналит, в наборе стабилен |
| `glucose_fasting` | `mmol/L` | 3.5 … 9.0 | чувствительна к недосыпу и стрессу |
| `cortisol_morning` | `nmol/L` | 100 … 900 | зависит от времени забора — всегда смотреть `observed_at` |
| `hemoglobin` | `g/L` | 100 … 180 | медленная динамика |

---

## 4. Контракт адаптеров

Общие требования ко всем адаптерам:

1. **Локальные сутки, не UTC.** Границы суток и ночи считаются в `tz` пользователя на этот день.
   Ночь относится к дате **пробуждения**.
2. **`missing_reason` обязателен** для каждой строки с `value=null`. Адаптер, который не может
   отличить «не носил» от «не синхронизировалось», ставит `not_synced` и не выдумывает.
3. **Интерполяция при импорте запрещена.** Если источник сам вернул восстановленное значение,
   ставится `method=imputed` и это фиксируется в `method_detail`. Молча заполнять дыры средним —
   нельзя: аналитика потеряет возможность оценить реальное покрытие.
4. **`record_id` детерминирован**: `{subject_id}:{date}:{metric}:{source}`. Повторный прогон
   адаптера над той же выгрузкой обязан дать те же ключи.
5. **Значения вне диапазона** из раздела 3 не отбрасываются: пишется `value=null`,
   `missing_reason=out_of_range`, а исходное значение сохраняется в `method_detail`.

### 4.1. Apple Health

Источник: XML-экспорт `export.xml` или HealthKit напрямую. `source=apple_health`.

| HealthKit identifier | metric | unit | method | method_detail |
|---|---|---|---|---|
| `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` | `hrv_sdnn` | `ms` | `measured` | `sdnn_spot_check_60s` |
| `HKQuantityTypeIdentifierRestingHeartRate` | `resting_hr` | `bpm` | `derived` | `apple_daily_resting_estimate` |
| `HKQuantityTypeIdentifierStepCount` | `steps` | `count` | `aggregated` | `sum_local_day` |
| `HKQuantityTypeIdentifierActiveEnergyBurned` | `active_energy_kcal` | `kcal` | `aggregated` | `sum_local_day` |
| `HKCategoryTypeIdentifierSleepAnalysis` | `sleep_*` | см. словарь | `aggregated` | `apple_sleep_stages_merged` |
| `HKQuantityTypeIdentifierBodyMass` | `body_mass_kg` | `kg` | `measured` | `scale_sync` |

Особенности, которые адаптер обязан учесть:

- **`hrv_sdnn`, а не `hrv_rmssd`.** Складывать с WHOOP нельзя (см. 2.1).
- Один и тот же интервал приходит **несколькими перекрывающимися сэмплами** от разных устройств
  (часы + телефон + сторонние приложения). Дедупликация — по `sourceName` + интервалу, приоритет
  у Apple Watch. Наивная сумма даёт двойной счёт шагов.
- Шаги приходят с телефона и переживают разряд часов — поэтому в наборе бывают дни, где `steps`
  есть, а ночных метрик нет.

### 4.2. WHOOP

Источник: WHOOP API v2. `source=whoop`.

| поле API | metric | преобразование | method | method_detail |
|---|---|---|---|---|
| `recovery.score.hrv_rmssd_milli` | `hrv_rmssd` | делить на 1000 → мс | `measured` | `rmssd_slow_wave_sleep_5min` |
| `recovery.score.resting_heart_rate` | `resting_hr` | как есть | `measured` | `whoop_sleep_min_hr` |
| `recovery.score.recovery_score` | `readiness_score` | как есть | `derived` | `whoop_recovery_v2` |
| `recovery.score.spo2_percentage` | `spo2_pct` | как есть | `measured` | `whoop_night_average` |
| `recovery.score.skin_temp_celsius` | `skin_temp_deviation_c` | минус персональная база | `derived` | `deviation_from_30d_baseline` |
| `sleep.start` / `sleep.end` | `sleep_onset` / `sleep_end` | в минуты от локальной полуночи | `measured` | `whoop_sleep_boundaries` |
| `sleep.score.stage_summary.*` | `deep_sleep_min`, `rem_sleep_min`, `awakenings` | мс → мин | `measured` | `whoop_stage_summary` |
| `sleep.score.sleep_efficiency_percentage` | `sleep_efficiency_pct` | как есть | `derived` | `whoop_efficiency` |
| `cycle.score.strain` | `workout_min` | **не мапится напрямую** | — | — |

Особенности:

- `hrv_rmssd_milli` приходит в **микросекундах несмотря на имя поля** в части версий API —
  адаптер обязан проверять порядок величины и логировать несоответствие, а не молча делить.
- WHOOP различает основной сон и дневной сон (`nap: true`). В `sleep_*` идёт только основной;
  дневной — отдельная метрика, если понадобится.
- Ночь у WHOOP привязана к «циклу», граница цикла может не совпадать с локальной полночью.
  Правило раздела 4.1 (ночь → дата пробуждения) имеет приоритет.

### 4.3. Кольцо Сбера

Источник: вендорский API. `source=sber_ring`. **Таблица — заглушка, поля уточняются при
подключении**; зафиксированы только целевые метрики и `method`, чтобы адаптер писался под уже
готовый контракт.

| поле API | metric | method | method_detail | статус |
|---|---|---|---|---|
| `?` | `hrv_rmssd` | `measured` | `ring_rmssd_night_average` | уточнить окно усреднения |
| `?` | `resting_hr` | `measured` | `ring_night_min_hr` | уточнить: минимум или среднее |
| `?` | `sleep_*` | `measured` | `ring_sleep_stages` | уточнить состав фаз |
| `?` | `respiratory_rate` | `measured` | `ring_night_average` | — |
| `?` | `steps` | `aggregated` | `sum_local_day` | пересекается с Apple Health, нужен приоритет |
| `?` | `readiness_score` | `derived` | `ring_readiness_v1` | шкала может быть не 0–100, привести |

Что заранее известно и что адаптер обязан обработать:

- **Систематическое смещение относительно WHOOP.** Кольцо и наручный трекер меряют в разных
  местах и усредняют по разным окнам; на одном человеке ряды расходятся на устойчивую величину.
  Смещение оценивается на окне параллельного ношения и **фиксируется в отчёте**, но значения в
  хранилище не корректируются — обе строки лежат как есть (см. 2.3).
- **Ряд начинается в середине наблюдения.** Дней до первой синхронизации быть не должно — не
  строки с `null`, а отсутствие строк (см. 2.2).
- Шкала `readiness_score` приводится к 0–100 на этапе адаптера, с записью исходной шкалы в
  `method_detail`.

---

## 5. Пример записей

CSV (заголовок + четыре строки: обычная ночь, пропуск, лаборатория, дубль от второго источника):

```csv
subject_id,record_id,date,period,observed_at,tz,tz_offset_min,metric,value,unit,source,source_device,method,method_detail,quality,missing_reason
synt-001,synt-001:2026-01-05:hrv_rmssd:whoop,2026-01-05,night,2026-01-05T06:24:00+03:00,Europe/Moscow,180,hrv_rmssd,33.9,ms,whoop,WHOOP 4.0,measured,rmssd_slow_wave_sleep_5min,0.78,
synt-001,synt-001:2026-01-17:hrv_rmssd:whoop,2026-01-17,night,,Europe/Moscow,180,hrv_rmssd,,ms,whoop,WHOOP 4.0,measured,rmssd_slow_wave_sleep_5min,,battery
synt-001,synt-001:2026-01-08:ferritin:lab,2026-01-08,point,2026-01-08T08:30:00+03:00,Europe/Moscow,180,ferritin,28.0,ng/mL,lab,,measured,venous_immunoassay,1.0,
synt-001,synt-001:2026-03-09:hrv_rmssd:sber_ring,2026-03-09,night,2026-03-09T06:35:00+03:00,Europe/Moscow,180,hrv_rmssd,43.7,ms,sber_ring,Sber Ring 1,measured,ring_rmssd_night_average,0.86,
```

Четыре строки показывают четыре ситуации: обычная ночь; пропуск из-за разряда
(`value` пуст, `missing_reason` заполнен, `observed_at` пуст); лабораторная точка
с `period=point`; и тот же `hrv_rmssd` от второго источника в день, когда кольцо
уже подключено, — обе строки живут рядом и не мержатся.

JSONL — первая из них объектом:

```json
{"subject_id":"synt-001","record_id":"synt-001:2026-01-05:hrv_rmssd:whoop","date":"2026-01-05","period":"night","observed_at":"2026-01-05T06:24:00+03:00","tz":"Europe/Moscow","tz_offset_min":180,"metric":"hrv_rmssd","value":33.9,"unit":"ms","source":"whoop","source_device":"WHOOP 4.0","method":"measured","method_detail":"rmssd_slow_wave_sleep_5min","quality":0.78,"missing_reason":null}
```

---

## 6. Чек-лист валидности набора

Набор данных считается корректным, если:

- [ ] каждая `metric` присутствует в словаре раздела 3, и `unit` совпадает со словарным;
- [ ] у каждой строки с `value=null` заполнен `missing_reason`, и наоборот;
- [ ] `record_id` уникален в пределах файла;
- [ ] `source` вправе писать эту метрику согласно колонке «источники» словаря;
- [ ] `period` совпадает со словарным для этой метрики;
- [ ] у всех строк с `period=point` заполнен `observed_at`;
- [ ] `tz_offset_min` согласован с офсетом внутри `observed_at`;
- [ ] значения вне диапазона помечены `out_of_range`, а не лежат как есть;
- [ ] нет строк с `value=0` там, где по смыслу подразумевался пропуск.
