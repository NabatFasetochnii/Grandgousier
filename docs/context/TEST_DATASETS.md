# Контрольные наборы данных

Локальные пути приведены для воспроизводимости на рабочей машине и не должны использоваться как переносимые настройки программы.

## RoboPhot BK Lyn — 2026-03-28, i

Основной regression dataset для candidate-excluded ensemble:

```text
path = C:\Users\nikita\Desktop\28.03.2026\CALIBRATED\i
series_id = 5
field_name = BK_Lyn
instrument = RoboPhot
tube = APM-RoboPhot
camera = ML0742515
filter = i
n_frames = 70
frame_ids = 192..261
catalog = auto_catalog_robophot_20260402_BK_Lyn_i
```

BK Lyn:

```text
Gaia DR3 source_id = 702296666944246784
catalog_source_id = 31299
object_id = 6751
```

Ожидаемое поведение: accepted candidate с origin `low_amplitude_intrinsic`; convergence mode доходит как минимум до нескольких дополнительных проходов и сохраняет BK Lyn в export.

## RoboPhot BK Lyn — 2026-04-02, i

Контрольная нормальная серия:

```text
path = C:\Users\nikita\Desktop\02.04.2026\CALIBRATED\i
series_id = 3
n_frames = 69
catalog = auto_catalog_robophot_20260402_BK_Lyn_i
```

Ожидаемое поведение: forced-only workflow работает без detection branch; BK Lyn находится как `intrinsic_variability` при соответствующей candidate policy.

## RoboPhot GPX-TF16E-48 — 2023-11-05, i

Негативный QC dataset:

```text
path = C:\PycharmProjects\GPX\photometry\obs\robophot\2023-11-05 GPX-TF16E-48\i
n_frames = 86
```

Серия имеет выраженные проблемы качества. При `--exclude-bad-pre-qc-frames` ожидается исключение 86/86 кадров и корректное завершение без повторного захвата всех кадров downstream:

```text
status = DONE_WITH_WARNINGS
science_status = BAD_FRAME_QC
science frames = 0
```
