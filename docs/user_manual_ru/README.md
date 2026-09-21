# Русскоязычное пользовательское руководство Grandgousier

Это основной пользовательский manual проекта. Он намеренно отделён от developer-context, release notes и исторических `README_MVP*.md`.

## Структура

Руководство построено по обычной схеме документации Python-пакета:

1. быстрый старт;
2. установка и настройка;
3. основные понятия и модель рабочего процесса;
4. подробное описание реализованной фотометрии одной серии;
   В этой главе приведены фактически реализованные формулы aperture photometry, error budget, aperture correction, frame ZP, ensemble calibration и color/reference transform;
5. команды CLI и типовые рабочие сценарии;
6. продукты, QC и воспроизводимость;
7. batch-обработка;
8. межсерийная стадия Stage 3;
9. raw/archive reduction workflow;
10. диагностика проблем;
11. стабильный Python API;
12. production service/job layer;
13. физическая идентичность наблюдений;
14. справочник команд и ссылки.

## Сборка

Основной движок — XeLaTeX.

```powershell
cd docs\user_manual_ru
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

При наличии `latexmk`:

```powershell
latexmk -xelatex -interaction=nonstopmode -halt-on-error main.tex
```

Сгенерированный PDF не требуется хранить в Git или включать в patch-only архив: источником истины являются `.tex`-файлы.

### Шрифты

На Windows/MiKTeX руководство сначала использует стандартные системные шрифты `Times New Roman`, `Arial` и `Consolas`, включая явные Cyrillic-family definitions для Polyglossia. На других системах предусмотрены fallback-наборы `Liberation` и `DejaVu`. Это сделано специально, чтобы MiKTeX не пытался генерировать отсутствующие Noto/Liberation fonts через `makemf`/`hbf2gf`.

## Правило сопровождения

Каждый будущий патч Grandgousier должен отдельно оценивать влияние на пользовательскую документацию. Если патч меняет CLI, команды, значения параметров, поведение pipeline, пользовательские продукты, обязательные migrations, поддерживаемый workflow или интерпретацию результатов, соответствующие `.tex`-файлы должны обновляться в том же patch-only архиве.

Внутренние оптимизации, которые не меняют пользовательский контракт, не требуют изменения текста manual; при необходимости достаточно обновить номер версии/краткое примечание.
