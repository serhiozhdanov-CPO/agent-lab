# Формат записи данных о здоровье и режиме

Единый контракт, в который пишут все источники: генератор синтетики
(`generate.py`), а в дальнейшем — адаптеры Apple Health, WHOOP и кольца Сбера.
Потребители (аналитика, дашборды) читают только этот формат и ничего не знают
о вендорах.

## 1. Модель данных

**Одна строка = один показатель за один день от одного источника.** Длинный
(narrow) формат: новая метрика или новый вендор не требуют миграции схемы —
добавляется значение в словарь, а не колонка в таблицу.

Ключ уникальности — `(date, metric, source)`. Два устройства могут прислать
свою версию одного показателя за один день: это не ошибка, это нормальная
ситуация, которая разрешается правилами приоритета (раздел 5).

**Физиологический день.** Сон относится ко дню *пробуждения*: запись
`sleep_start = 23:40` с датой `2026-03-14` означает, что человек лёг вечером
13 марта и проснулся 14-го. Ночные метрики (пульс покоя, вариабельность,
частота дыхания, температура) измеряются во время этого же сна и получают ту же
дату. Поля `window_start` / `window_end` снимают всякую двусмысленность —
там лежат настоящие границы измерения с часовым поясом.

Пропуск — это **отсутствующая строка**. Не `0`, не `null`, не интерполяция.

## 2. Поля

Порядок колонок в CSV зафиксирован; в JSONL те же имена — по одному объекту на
строку.

| поле | тип | обязательное | описание |
|---|---|---|---|
| `date` | `YYYY-MM-DD` | да | календарный день, к которому отнесён показатель |
| `metric` | идентификатор | да | из словаря раздела 3 |
| `value` | число или `HH:MM` | да | значение; `HH:MM` только для `unit = clock` |
| `unit` | enum | да | единица из словаря; приводится источником, не потребителем |
| `source` | enum | да | `apple_health`, `whoop`, `sber_ring`, `lab_generic`, `synthetic` |
| `method` | enum | да | как получено значение, раздел 4 |
| `source_device` | строка | да | `apple_watch_s9`, `iphone`, `whoop_4`, `sber_ring_1`, `venous_draw` |
| `timezone` | IANA | да | часовой пояс, в котором человек находился |
| `window_start` | ISO-8601 со смещением | да | начало окна измерения |
| `window_end` | ISO-8601 со смещением | да | конец окна; для точечных замеров равен `window_start` |
| `quality` | `0.00`–`1.00` | да | полнота покрытия и доверие к записи |
| `record_id` | 16 hex | да | `sha1(date\|metric\|source)[:16]` — идемпотентная загрузка |
| `ingested_at` | ISO-8601 со смещением | да | когда запись попала в хранилище |
| `note` | строка | нет | свободный комментарий; пустая строка по умолчанию |

`source_device` нужен именно потому, что Apple Health присылает один и тот же
показатель от часов и от телефона: без него дубли не различить.

## 3. Словарь метрик

Вендорские метрики без кросс-вендорного смысла живут в своём пространстве имён
(`whoop.`, `sber.`) — их нельзя сравнивать между источниками, потому что за ними
стоят закрытые формулы.

### Сон и режим

| метрика | единица | точность | диапазон | кто отдаёт |
|---|---|---|---|---|
| `sleep_start` | `clock` | минута | `HH:MM` | whoop, sber_ring, apple_health |
| `sleep_end` | `clock` | минута | `HH:MM` | whoop, sber_ring, apple_health |
| `sleep_duration` | `min` | 1 мин | 120–720 | whoop, sber_ring, apple_health |
| `sleep_efficiency` | `%` | 0.1 | 60–100 | whoop, sber_ring |
| `awakenings` | `count` | 1 | 0–15 | whoop, sber_ring |

### Ночная физиология

| метрика | единица | точность | диапазон | кто отдаёт |
|---|---|---|---|---|
| `resting_hr` | `bpm` | 1 | 35–95 | whoop, sber_ring, apple_health |
| `hrv_rmssd` | `ms` | 1 | 5–200 | whoop, sber_ring |
| `hrv_sdnn` | `ms` | 1 | 5–200 | apple_health (**зарезервировано**, см. 5.3) |
| `respiratory_rate` | `brpm` | 0.1 | 8–25 | whoop, sber_ring |
| `temp_deviation` | `celsius` | 0.01 | −2.0…+2.5 | sber_ring, whoop |

`temp_deviation` — отклонение от личной базовой линии, а не абсолютная
температура. Абсолютные значения устройства не измеряют.

### Активность и тело

| метрика | единица | точность | диапазон | кто отдаёт |
|---|---|---|---|---|
| `steps` | `count` | 1 | 0–60000 | apple_health |
| `active_energy` | `kcal` | 1 | 0–4000 | apple_health |
| `workout_minutes` | `min` | 1 | 0–400 | apple_health |
| `weight` | `kg` | 0.1 | 30–200 | apple_health (ручной ввод или весы) |

### Вендорские сводные оценки

| метрика | единица | точность | диапазон | кто отдаёт |
|---|---|---|---|---|
| `whoop.recovery_score` | `%` | 1 | 1–99 | whoop |
| `whoop.strain` | `score` | 0.1 | 0–21 | whoop |
| `sber.readiness` | `%` | 1 | 1–99 | sber_ring |

### Лаборатория

| метрика | единица | точность | референс* |
|---|---|---|---|
| `lab_crp` | `mg/L` | 0.01 | < 5 |
| `lab_ferritin` | `ng/mL` | 0.1 | 30–300 |
| `lab_vitamin_d` | `ng/mL` | 0.1 | 30–100 |
| `lab_hba1c` | `%` | 0.1 | < 5.7 |
| `lab_glucose_fasting` | `mmol/L` | 0.01 | 3.9–5.6 |
| `lab_tsh` | `mIU/L` | 0.01 | 0.4–4.0 |

\* Ориентировочные интервалы для правдоподобия синтетики. Реальные референсы
задаёт лаборатория, и адаптер обязан переносить их из выгрузки, а не подставлять
из этой таблицы.

### Служебное

| метрика | единица | описание |
|---|---|---|
| `data_gap` | `count` | маркер пропуска, `value = 0`, причина в `note` |

## 4. Значения `method`

| `method` | смысл |
|---|---|
| `device_measured` | устройство измерило показатель напрямую |
| `device_derived` | устройство посчитало из своих же сырых данных (стадии сна, пульс покоя) |
| `vendor_algorithm` | закрытая формула вендора; **между вендорами не сравнивается** |
| `aggregated_daily` | адаптер свернул поток сэмплов в одно суточное значение |
| `manual_entry` | человек ввёл руками |
| `lab_assay` | лабораторный анализ |
| `imputed` | значение достроено; в синтетике так помечены только строки `data_gap` |

Разница между `device_measured` и `vendor_algorithm` — принципиальная.
`whoop.recovery_score` и `sber.readiness` считаются по разным закрытым формулам
из разных входов; их числа не сопоставимы, даже если обе метрики выражены в
процентах. Сравнивать можно только измеренные величины: `hrv_rmssd`,
`resting_hr`, `sleep_duration`.

## 5. Правила для адаптеров

### 5.1 Соответствие полей источника и метрик

| источник | поле источника | метрика | `method` |
|---|---|---|---|
| Apple Health | `HKQuantityTypeIdentifierRestingHeartRate` | `resting_hr` | `aggregated_daily` |
| Apple Health | `HKQuantityTypeIdentifierRespiratoryRate` | `respiratory_rate` | `aggregated_daily` |
| Apple Health | `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` | `hrv_sdnn` | `aggregated_daily` |
| Apple Health | `HKCategoryTypeIdentifierSleepAnalysis` | `sleep_start`, `sleep_end`, `sleep_duration` | `device_derived` |
| Apple Health | `HKQuantityTypeIdentifierStepCount` | `steps` | `aggregated_daily` |
| Apple Health | `HKQuantityTypeIdentifierActiveEnergyBurned` | `active_energy` | `aggregated_daily` |
| Apple Health | `HKQuantityTypeIdentifierBodyMass` | `weight` | `manual_entry` |
| WHOOP | `recovery.score.resting_heart_rate` | `resting_hr` | `device_derived` |
| WHOOP | `recovery.score.hrv_rmssd_milli` | `hrv_rmssd` | `device_measured` |
| WHOOP | `recovery.score.recovery_score` | `whoop.recovery_score` | `vendor_algorithm` |
| WHOOP | `cycle.score.strain` | `whoop.strain` | `vendor_algorithm` |
| WHOOP | `sleep.score.stage_summary` | `sleep_*`, `awakenings` | `device_derived` |
| Кольцо Сбера | суточная сводка «готовность» | `sber.readiness` | `vendor_algorithm` |
| Кольцо Сбера | суточная сводка «температура» | `temp_deviation` | `device_measured` |
| Кольцо Сбера | суточная сводка «пульс покоя» | `resting_hr` | `device_derived` |

### 5.2 Агрегация

Apple Health отдаёт поток сэмплов, а не суточные значения. Свёртку делает
адаптер и обязан пометить её `method = aggregated_daily`, а окно свёртки
положить в `window_start` / `window_end`. Правило свёртки фиксируется в коде
адаптера, а не подразумевается: пульс покоя — минимум скользящего среднего за
ночное окно, шаги — сумма за календарные сутки в местном часовом поясе.

### 5.3 Единицы и ловушки приведения

Приводит **источник**, не потребитель.

- Длительность сна — всегда минуты. WHOOP отдаёт миллисекунды.
- `hrv_rmssd` — всегда миллисекунды.
- **Apple Health отдаёт SDNN, а не RMSSD.** Это разные метрики, и класть SDNN
  в `hrv_rmssd` нельзя: получится ряд, который скачет при смене устройства.
  Для этого заведена отдельная метрика `hrv_sdnn`; генератор её не производит.
- Температура — отклонение от базовой линии, а не градусы по Цельсию.

### 5.4 Дедупликация внутри одного источника

Apple Health присылает шаги и от часов, и от телефона. Строки различаются полем
`source_device`; при сборке суточного значения адаптер выбирает один трек
(часы приоритетнее телефона) и не складывает их.

### 5.5 Приоритет источников

Если на одну пару `(date, metric)` пришло несколько строк от разных источников,
потребитель **выбирает одну по приоритету**, а не усредняет:

| метрика | приоритет |
|---|---|
| `hrv_rmssd`, `resting_hr`, `sleep_*`, `respiratory_rate` | `whoop` > `sber_ring` > `apple_health` |
| `temp_deviation` | `sber_ring` > `whoop` |
| `steps`, `active_energy`, `workout_minutes`, `weight` | `apple_health` > остальные |
| `lab_*` | `lab_generic` |

Усреднение источников с разной калибровкой даёт ряд с искусственными скачками в
дни, когда одно из устройств не носили. Реализация правила — функция
`load_series` в `generate.py`.

## 6. Пропуски

Пропуск фиксируется отсутствием строки. По желанию источник может дополнительно
записать маркер `data_gap` с причиной в `note`:

| `note` | смысл |
|---|---|
| `not_worn` | устройство не носили |
| `battery_dead` | разрядилось |
| `sync_failed` | не синхронизировалось |
| `bad_contact` | контакт датчика потерян, часть метрик за ночь отсутствует |

Маркеры пишутся только по флагу `--emit-gaps`: аналитика должна одинаково
корректно работать и без них.

## 7. Примеры

CSV — обычный день, где пульс покоя пришёл от двух устройств сразу:

```
date,metric,value,unit,source,method,source_device,timezone,window_start,window_end,quality,record_id,ingested_at,note
2026-02-19,sleep_start,00:26,clock,whoop,device_derived,whoop_4,Asia/Shanghai,2026-02-19T00:26:24+08:00,2026-02-19T05:52:04+08:00,0.78,090c9a42f501ff3f,2026-02-20T06:15:00+08:00,
2026-02-19,sleep_duration,265,min,whoop,device_derived,whoop_4,Asia/Shanghai,2026-02-19T00:26:24+08:00,2026-02-19T05:52:04+08:00,0.78,84601a5effb65b89,2026-02-20T06:15:00+08:00,
2026-02-19,hrv_rmssd,32,ms,whoop,device_measured,whoop_4,Asia/Shanghai,2026-02-19T00:26:24+08:00,2026-02-19T05:52:04+08:00,0.78,f4b2db9d8d44af7b,2026-02-20T06:15:00+08:00,
2026-02-19,resting_hr,62,bpm,sber_ring,device_derived,sber_ring_1,Asia/Shanghai,2026-02-19T00:26:24+08:00,2026-02-19T05:52:04+08:00,0.78,90423f3845340ea8,2026-02-20T06:15:00+08:00,ring calibration differs from whoop
2026-02-19,resting_hr,61,bpm,whoop,device_derived,whoop_4,Asia/Shanghai,2026-02-19T00:26:24+08:00,2026-02-19T05:52:04+08:00,0.78,2d23d46e87ec6ee6,2026-02-20T06:15:00+08:00,
2026-02-19,steps,10480,count,apple_health,aggregated_daily,iphone,Asia/Shanghai,2026-02-19T00:00:00+08:00,2026-02-20T00:00:00+08:00,0.78,0c59ca0ca2c4ae4c,2026-02-20T06:15:00+08:00,
```

Две строки `resting_hr` за 19 февраля — не конфликт, а штатная ситуация:
по правилу 5.5 берётся значение WHOOP, значение кольца остаётся в данных.

JSONL — лабораторная точка и маркер пропуска:

```json
{"date": "2026-04-06", "ingested_at": "2026-04-07T06:15:00+03:00", "method": "lab_assay", "metric": "lab_crp", "note": "", "quality": "1.00", "record_id": "a28d9c346d0bb8de", "source": "lab_generic", "source_device": "venous_draw", "timezone": "Europe/Moscow", "unit": "mg/L", "value": "11.11", "window_end": "2026-04-06T08:40:00+03:00", "window_start": "2026-04-06T08:40:00+03:00"}
{"date": "2026-01-23", "ingested_at": "2026-01-24T06:15:00+03:00", "method": "imputed", "metric": "data_gap", "note": "battery_dead", "quality": "1.00", "record_id": "0c1b907144ecc157", "source": "whoop", "source_device": "whoop_4", "timezone": "Europe/Moscow", "unit": "count", "value": "0", "window_end": "2026-01-24T00:00:00+03:00", "window_start": "2026-01-23T00:00:00+03:00"}
```

## 8. Инварианты

Любой писатель в формат обязан их держать; любой читатель вправе на них
опираться.

1. `(date, metric, source)` уникальна во всём наборе.
2. `record_id` уникален и выводится только из этой тройки.
3. `metric` есть в словаре раздела 3; `unit` совпадает с указанной для метрики.
4. `method` и `source` — из перечислений разделов 2 и 4.
5. `window_end >= window_start`; оба со смещением часового пояса, согласованным
   с полем `timezone`.
6. `0.00 <= quality <= 1.00`.
7. `value` парсится как число, кроме `unit = clock` — там строго `HH:MM`.
8. Пропущенный день — отсутствие строки. Ноль всегда означает измеренный ноль
   (например, `workout_minutes = 0` — это день отдыха, а не потеря данных).

Проверить набор на соответствие инвариантам 1–7 можно тем же кодом, что читает
данные: `load_series` в `generate.py` падает на нарушении словаря и молча
разрешает дубли только по правилу приоритета.
