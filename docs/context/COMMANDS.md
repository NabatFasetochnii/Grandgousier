# Рабочие команды

## Окружение

```powershell
$env:GRANDGOUSIER_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/grandgousier"
```

## BK Lyn 2026-03-28: until convergence

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\Users\nikita\Desktop\28.03.2026\CALIBRATED\i" `
  --execute `
  --catalog-name "auto_catalog_robophot_20260402_BK_Lyn_i" `
  --target-max-sep-arcsec -1 `
  --no-target-light-curve `
  --working-catalog-mag-limit 19.0 `
  --max-working-sources-per-frame 2000 `
  --catalog-edge-margin-pix 20 `
  --exclude-bad-pre-qc-frames `
  --enable-detection-branch `
  --exclude-candidates-from-ensemble `
  --candidate-ensemble-until-converged `
  --candidate-ensemble-convergence-max-iterations 10 `
  --manifest-out reports\check_05333_bklyn_20260328_i_candidate_excluded_until_converged.json
```

## Проверка полей manifest

```powershell
$manifest = Get-Content `
  reports\check_05333_bklyn_20260328_i_candidate_excluded_until_converged.json `
  -Raw | ConvertFrom-Json

$qp = $manifest.pipeline_results[0].quality_policy
$qp | Select-Object `
  candidate_ensemble_iteration_mode, `
  candidate_ensemble_converged, `
  candidate_ensemble_stop_reason, `
  candidate_ensemble_actual_exclusions_iteration, `
  candidate_ensemble_requested_excluded_objects, `
  candidate_ensemble_actual_excluded_reference_objects
```

## Автоматические проверки

```powershell
python -m compileall grandgousier tests scripts
python -m pytest -q
```

## Создание контекста для нового чата

```powershell
python scripts\build_context_bundle.py `
  --manifest reports\check_05333_bklyn_20260328_i_candidate_excluded_until_converged.json
```

## Чистый архив исходников

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_clean_source.ps1
```
