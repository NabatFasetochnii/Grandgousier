# Grandgousier — текущая и одобренная целевая архитектура
## 2026-10-23 Target-optional resolver correction — local focused tests PASS; live/science validation OPEN
- **Current implementation; current behavior supersedes earlier target-resolution descriptions for implementation behavior only.** After paired-coordinate validation and the existing explicit-empty-frame / negative-limit checks, no object ID, catalog source ID, or paired caller coordinates returns `method="not_requested"` before calibration, frame-instrument, or header DB queries. Frame pointing and header cards—including MASTER `ALPHA/DELTA`, `CRVAL/WCS`, and `coord2000`—never identify a target. The MASTER profile's unused `ra_requested` / `dec_requested` aliases are removed; pointing aliases and the separate Gaia catalog-search-center behavior are unchanged.
- No database schema field or migration was added; this patch does add paired RA/Dec runner configuration/CLI inputs and `coordinate_provenance` in resolver output. Existing policy fields distinguish `not_requested` / `UNRESOLVED_NO_TRUSTED_TARGET_INPUT` from explicit-but-unresolved selector provenance and resolution method/reason. Explicit ID, catalog-ID, and paired-coordinate routes remain available.
- Ordinary field runs no longer automatically invoke `cleanup_target_light_curve_artifacts`; the helper was left unchanged and uncalled. Target export still requires resolution. Synthetic runner/QC tests cover empty target/reference IDs, successful non-failure status, no export, byte-preserved target-named files in `tmp_path`, and no `FIELD_TARGET` flag for empty target IDs.
- Reference provenance remains unchanged: static source evidence shows ensemble result serialization of `reference_object_ids`, persisted normalized `is_reference`, and object/frame/series/calibration identity on target LC rows. Whether live iteration rows are present and always unambiguously join to exports is **UNVERIFIED**; no new membership feature was introduced.
- **Focused local test results:** combined field-target/runner/QC/archive-path/Stage-1 command: `112 passed, 1 deselected` (deselected/excluded: real-MASTER inventory test `test_real_master_inventory_exact_gpx_v25_read_only_if_available`). Full pytest and full Ruff were not run. No DB/network/private config, real CLI/DB-backed pipeline or archive execution, product access/write, cleanup, schema/formula/materialization change, or commit. `READY_FOR_STAGE2_SCIENCE=NO`, `READY_FOR_LIVE_MASTER_DB=NO`, `READY_FOR_MASTER_PRODUCTION=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_CLEANUP=NO`, `READY_FOR_COMMIT=NO`.
- **Readiness and science boundary:** a field can be processed without a target; field-processing readiness is distinct from optional target-product readiness. `READY_FOR_STAGE2_SCIENCE=NO` here is due to the separately documented RoboPhot r ensemble-contamination/science STOP (object 2275 remained a reference), not merely a missing selector; no new science acceptance is claimed.
- This workflow/identity phase changed no photometry, aperture/centroid/correction, ensemble/calibration or uncertainty formulas, Stage 2 thresholds, or Stage 3 mathematics.
- MASTER `coord2000` is optional archive/search/frame metadata that varies by frame, not target identity. The current parenthesized `coord2000::text` value is not parsed by the Stage 1 parser; its final semantics/conversion are open. This is an archive/search metadata limitation, not a global field-science blocker.
- **Storage direction:** one calibrated field supports multiple downstream object selections; full Stage 2 is not rerun per target. Immutable BASE and current validated SCIENCE are durable; heavy temporary/superseded products need a bounded lifecycle. Reduced Stage 1 FITS are reclaimable only under existing policy after durable Stage 2 validation.
- **NEXT (audit only):** audit one-command bounded processing of the existing complete MASTER archive: MASTER inventory → canonical observation dedup → bounded Stage 1 batches → complete series → Stage 2 → durable science → controlled reclaim → Stage 3/final products. No processing design or implementation in this phase; earlier dated PLAN/NEXT entries below remain historical and are not the current next step.

## 2026-10-22 MASTER requested-target header resolution — local regression PASS; live integration UNVERIFIED
- **IMPLEMENTED_NOT_VALIDATED for live MASTER DB integration:** MASTER may resolve a target only from a fully verified all-MASTER selected frame scope with no explicit object/source ID or paired caller coordinates. It reads the exact scope's persisted `Frame.header_json`, requires `GGSTAGE='1B'`, boolean `GGMSMETA is True`, nonempty `GGHDR`, and a valid, exact-unanimous requested-coordinate pair from only profile aliases `ra_requested` / `dec_requested`. `ALPHA` sexagesimal text follows the profile parser; raw numeric `ALPHA` is rejected as unit-ambiguous; numeric `RA` uses the verified profile/parser degree convention. Conflict, partial/malformed/out-of-range values or disagreement on any selected frame fail closed. `coord2000`, center columns, CRVAL/WCS and nearest-center logic are never target authority; RoboPhot never uses the MASTER helper.
- **Source evidence only:** the MASTER profile declares `[ALPHA, RA]` and `[DELTA, DEC]` as requested aliases; the existing Stage 1 processing-header copy preserves nonstructural cards and stamps `GGSTAGE='1B'` / `GGMSMETA=True` / `GGHDR`; `archive_indexer` persists the final FITS header to `Frame.header_json`. A prior live MASTER probe returned `OperationalError` with cause/stage unknown; it was not retried. Live MASTER DB schema/content compatibility remains **UNVERIFIED**.
- Once trusted coordinates exist, resolution retains the unique measured-object match within the existing 2 arcsec contract and only exposes nearest candidates diagnostically. Explicit identity retains precedence; an ID-only measured Object or unique catalog link can resolve with absent positions, but simultaneous explicit RA/Dec still requires a possible comparison. Object-record coordinates are not labeled caller-supplied. Final run quality policy now distinguishes requested inputs from effective/resolved coordinates and records `master_database` / `master_database_processing_header` provenance.
- **VALIDATED, focused local regression suites:** `tests/test_target_lightcurve.py` 61 passed; `tests/test_master_archive_reduction.py` 16 passed (temporary FITS regression confirms ALPHA/DELTA/GGHDR/GGMSMETA survive the existing path); `tests/test_process_archive_path.py` 32 passed. F821 and scoped `git diff --check` passed. Scoped Ruff exited 1 with 13 findings on existing/untouched lint items; no unrelated lint cleanup was made. No DB, network, CLI, pipeline execution, Stage 1/2 smoke, schema/migration or production data access/write occurred.
- This source change is **not** MASTER live-DB integration or science acceptance: `READY_FOR_LIVE_MASTER_DB_SCHEMA=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_PRODUCTION=NO`, `READY_FOR_COMMIT=NO`. The dated RoboPhot/TEST evidence below remains historical and is not changed or generalized to RoboPhot.

## 2026-10-22 RoboPhot target identity - r ensemble contamination demonstrated, science STOP
- **VALIDATED:** Reviewer independently inspected 74 real RoboPhot FITS (37 i / 37 r): no requested target pair; varying `ALPHA/DELTA` is telescope scope pointing, `CRVAL1/2` WCS frame reference. Old target i/r objects 2425/2920 are 202.78/198.03 arcsec from candidate object 2275; mean frame center is 191.12/207.25 arcsec away. Candidate CSV object 2275 / source `531486745299867904`: accepted 37/37 per band, paired Pearson 0.958262; i/r median 14.130775/14.053411 and RMS 0.073063/0.073771 mag. It is **not** a newly corrected FIELD_TARGET product.
- **VALIDATED TEST READ ONLY, distinct from source patch:** guarded same-backend non-frozen `grandgousier_070_gpx_full` OID `23069087`, 74 scoped frame identities (37/37), one exact object-2275 catalog link with epoch 2016 positions and 0.000000 arcsec separation, one nearby measured candidate within 2 arcsec; two BASE flag samples and separate bounded 74 BASE rows with consistent source identity and `catalog_source_id=50282`. No new calibration/science acceptance. The catalog query code uses Gaia **DR3**, while `gpt.txt` labels this ID **DR2**; cross-release identity is unproven. A 5000 MAXREC warning and old i/r field-QC candidate triage (raw 176/169; RMS excess 166/157; intrinsic 51/53; low amplitude 5/12) are **historical**, not a completeness guarantee.
- **IMPLEMENTED in uncommitted source:** explicit paired degree target RA/Dec and scoped unique measured-object match (2 arcsec) or unique catalog-ID link with conflict refusal, provenance and instrument scope checks in `lightcurves/target.py`, `pipeline/runner.py`, `archive/auto_process.py`, `cli.py`; regression coverage in `tests/test_target_lightcurve.py` and `tests/test_process_archive_path.py` (all already dirty). RoboPhot and MASTER **both** refuse automatic nearest-frame-center target identity; MASTER archive-DB target lookup is not implemented, explicit ID/coords remain available. No target FITS header derivation, schema/migration/photometry-formula change, or live pipeline execution. Executor reported 75 focused tests (may include other tests); Reviewer independently 68 passed, F821/diff --check PASS; full Ruff **57 findings, individual pre-patch attribution not revalidated**; no full pytest/doctor.
- **NEW Reviewer guarded non-frozen TEST RR READ ONLY controls, separate checks:** object-QC on i SCIENCE CV214/unit3, r CV218/unit4 reports target 2275 37 points/filter, median 14.130775/14.053411 and sample RMS .073063/.073771 mag; nearest neighbor 31.55 arcsec; QC candidate controls 2298/2305 also report 37/filter, isolated >10 arcsec. Initial bounded SCIENCE-control attempt **STOPped at i**: object 2284 stored SCIENCE 36, QC n37; cause OPEN, not target mismatch or intrinsic failure. Separate three-object (2275,2298,2305) × 37-frame/filter SCIENCE check returned 111 rows/filter, target `mag_cal` vs existing candidate CSV maxabs 0, both controls 37/37. i control RMS .0090/.0087, corr target .279/-.183; r RMS .0107/.0125, corr -.116/.132; two mean-centered controls subtracted: target RMS .07277/.07405 i/r vs raw .07306/.07377. These are **provisional stored-science controls only**, not accepted-science QC, matched background/position/color causality, detection support or independent intrinsic-variability proof.
- **NEW separate guarded TEST source-chain check:** final SCIENCE CV214 i / CV218 r, ENSEMBLE iterations 182 i / 185 r. i requested and actually excluded wrong object **2425** (persisted `EXCLUDED_REFERENCE`, false); 2275 `NON_REFERENCE`, false. r requested wrong **2920**, actually excluded none because 2920 was not a reference (`REQUESTED_EXCLUSION`, false); target **2275 remained `REFERENCE`, true**. `runner.py:1728` forwards resolved target IDs to `exclude_reference_object_ids`. r target-reference membership is **demonstrably contaminated**; photometric bias is **UNKNOWN**. Changing just r target CSV leaves r calibration/derived products unchanged. i 2275 was not a reference, but excluding 2425 may have altered i reference ensemble; i SCIENCE validity/invalidity not established. Both old FIELD_TARGET assignments are wrong.
- **STOP / PLAN:** Existing CSV i/r magnitude/background Pearson -0.924/-0.823 and x .843/.805 may be time/trend confounded. Median local seeing 4.3823/4.2749 px, random errors .003985/.004824 mag, background 1517/1917.5; no systematic errors in those CSVs. Analyze i exclusion/2284 discrepancy and r fresh-name ensemble feasibility from existing BASE/normalized upstream measurements with explicit 2275 exclusion, plus zero points, downstream QC/COLOR/SCIENCE lineage and detection support, for series 19/20 only. The public `ensemble-calibrate` CLI has no reference-exclusion option; `process-archive-path` plan-only cannot recalculate and execution may mutate DB/unlink or overwrite output (`cleanup_target_light_curve_artifacts` on unresolved rerun). Older dated STOP in canonical docs is superseded **only for the source patch**, not science. No rerun/overwrite without Reviewer plan and current-session human approval. This docs-only edit has no DB contact/write, tests, product writes/deletions or commit; `READY_FOR_STAGE2_SCIENCE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXPORT_OVERWRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

## 2026-10-21 M5 source/plan boundary - STOP, not science validation
- No new worktree: `master` HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, grouped 98 (75 tracked/23 untracked groups), staged 0; existing `agent/luna-test` HEAD `0648cea58da83d777be0b2231cec65b8c7d5ea2e`, grouped 18 (8/10), staged 0; separate history, no copy/merge.
- Expanded `-uall` dated reads ~1.1k (1109/1112/1115); most untracked were `.opencode` (Reviewer saw 1012 then 1015). Exact change cause UNKNOWN; grouped 98 is the baseline, not an expanded-count invariant.
- Untracked checker `tools/check_m5_test_source_fields.py` SHA-256 `1024EA99F4396438AD378E6EC64008BD8EDABBEB993D7BF6D374C1DCC8EC7644`; historical B747→3365 provenance UNKNOWN; two later approved offline checker fixes reported, origin not inferred; seven selected source/test pins matched offline.
- Latest exact `STOP M5_TEST_CANDIDATE_PLAN_SIZE_RO stage=PRIVATE_SOURCE code=UNEXPECTED`, exit 2; pinned path/report put STOP before DB connection: 0 SELECT/EXPLAIN, no size/new plan. Private-source read and exact cause UNKNOWN.
- Architecture boundary: frame-term cardinality OPEN; earlier `field_series_frames SEQ_SCAN` is a planner access-path STOP, not science mismatch. Export 2640 = 2618 accepted + 22 rejected; QC22/numeric/source checks are distinct TEST snapshots, not full acceptance.
- Docs only, no DB/frozen contact. Further DB read needs new plan → Reviewer audit → scoped user approval; DB write/GPX execute/frozen write/release/commit NO. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-20 M5 FRAME TERM boundary — planner access-path STOP, cardinality OPEN/BLOCKED

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`; pre-edit Git **98** dirty (75 modified, 23 untracked incl. user work), staging **0**. Current untracked checker `tools/check_m5_test_source_fields.py` SHA-256 `B74798D32F7CFA221E9148AD975510C1099C3FBE68BBC270D503C7D43AD088CA` after approved checker-only edits/offline fakes/static guards; seven source/test pins matched. Earlier candidate PASS at checker `7439604C890B0919EF10F12046C9F09730EB52331C5D14022A5C7143501F6DB3` and QC22 22+1 selected object-local/histogram PASS at `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674` are distinct **dated** TEST snapshots, not new live/export-time rejected identities or full accepted science.

The user chose **KEEP** current EDGE merge policy: `measurement EDGE=False` + `object EDGE=True` → merged `EDGE=False`, `OBJECT_EDGE=True`; default reject EDGE ignores `OBJECT_EDGE`. Inline two-case synthetic demo confirmed code behavior; intended truth chosen, science-policy code unchanged. One newly approved OFFLINE no-follow read of the fixed original accepted CSV (27,774,120 bytes, 2,618 accepted; V/R 1,309 each) reported `EDGE`, `OBJECT_EDGE`, `FRAME_EDGE` keys absent on **all accepted rows only**; no conclusion for 22 omitted rows, current TEST or other objects. No product rewrite, published output IDs or raw-log read.

Three distinct separately approved/now **CONSUMED** guarded READ ONLY non-frozen PG18 TEST `grandgousier_070_gpx_full` expected OID `23069087` diagnostics: (1) exact `STOP M5_TEST_FRAME_TERM_CARDINALITY_RO phase=PLAN` / `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2, four-EXPLAIN stage/count **UNKNOWN**, no term-cardinality result; Executor offline postcheck `$Matches` collision exit 2. (2) exact `STOP M5_TEST_FRAME_TERM_PLAN_RO stage=CANDIDATE code=LARGE_SCAN_KIND` / `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2; pinned STOP path **implies** four bounded SELECTs + three non-ANALYZE EXPLAINs, no candidate/term-row SELECT. (3) latest exact `STOP M5_TEST_CANDIDATE_PLAN_SHAPE_RO stage=CANDIDATE code=LARGE_SCAN_KIND relation=field_series_frames scan=SEQ_SCAN` / `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2; pinned path **implies** four bounded SELECTs + one non-ANALYZE EXPLAIN, no candidate/term-row SELECT. No raw plan published; implications are **not independent server audits**. Reviewer independently **offline after latest STOP** matched HEAD/98, staging 0, checker SHA and seven pins.

Schema has unique `(calibration_iteration_id,frame_id,term_kind)`, while normalized reconstruction indexes terms by frame alone: frame-term cardinality still **OPEN/BLOCKED**; QC22 tested only selected 23 pairs. `LIMIT 2641` bounds output, not join work. A future exact `pg_relation_size` + structural EXPLAIN screening of whether `field_series_frames` sequential scan can safely be allowed is **PLAN-ONLY, UNEXECUTED**, not permission to run candidate/term SELECTs. These are **planner access-path** STOPs, not observed ambiguous terms, DB drift, science mismatch or proof of no ambiguity. All checker edits/child PG-name filtering/private-YAML/DB one-shots consumed; new plan → Reviewer audit → scoped user approval before execution/overwrite. No DB writes by READ ONLY contract; frozen untouched per guards/reports only (not server-verified), physical socket closure not independently proven and no product/output writes reported. This docs task reads no DB/network/private/product/log, edits no code, runs no science tests, deletes nothing and makes no commit. Frozen/release/full accepted QC/full iteration/COLOR still open. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-20 M5 current TEST candidate/QC22 boundary — bounded object-local PASS, science OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`; grouped dirty status **98** (75 modified + 23 untracked, including user work). The current untracked checker `tools/check_m5_test_source_fields.py` is SHA-256 `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674` (**127,093 bytes**); seven source/test pins matched. Earlier D28 checker/two-field claims below are dated history, not rewritten.

**Candidate identities, prior separate snapshot:** checker SHA-256 `7439604C890B0919EF10F12046C9F09730EB52331C5D14022A5C7143501F6DB3` reported exact whole-shell `PASS M5_TEST_CANDIDATE_KEYS_RO candidates=2640 accepted=2618 missing=22 scope=CURRENT_TEST_CANDIDATES_ONLY` then `NATIVE_EXIT=0 WRAPPER_EXIT=0`, native/wrapper/shell **0/0/0**. The approved, now consumed READ ONLY PASS path implies one same-backend P207-guarded PG18 non-frozen TEST `grandgousier_070_gpx_full` (expected OID `23069087`) RR transaction, three SELECTs, rollback/close before comparison. Current candidate difference is not historical rejected identity or normalized survivorship.

**Normalized-stage/QC replay, new snapshot:** checker SHA-256 `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674` reported exact whole-shell `PASS M5_TEST_CURRENT_CANDIDATE_QC_RO missing=22 control=1 scope=CURRENT_TEST_OBJECT_LOCAL_QC_ONLY` then `NATIVE_EXIT=0 WRAPPER_EXIT=0`, native/wrapper/shell **0/0/0**. Pinned PASS path implies one fixed original CSV/summary read, fixed private YAML read once, one guarded same-backend RR READ ONLY TEST connection and **eight top-level SELECTs**: P207 probe, 18-source metadata, 2,640 current candidate keys, 23 exact BASE/SCIENCE/frame/M4 points, at most 126 selected-chain nodes, two bounded selected frame/object term-pair queries and at most 23 object-QC requests. All **22 newly recomputed** difference rows survived the **selected object-local** normalized chain and were rejected under current QC; **global and per-series** reason histograms matched the fixed export summary (reasons may overlap); one accepted control matched the original CSV's three serialized QC fields. Durable SCIENCE scalars overlay only surviving states; final comparison follows rollback/close per code. This does not establish full-iteration COLOR completeness for other objects.

The checker-edit, child-only PG* environment-name filtering and guarded YAML/TEST permissions are consumed. Reviewer audited local source and independently after QC22 PASS checked **offline** HEAD/98, eight pins and only two regular original bundle files (CSV **27,774,120** / JSON **13,952** bytes), not a DB requery or product-content rescan. Socket closure not independently established, frozen not contacted per guards/reports only (not server-verified), no DB writes by READ ONLY contract and no reported product writes/deletions. The older single accepted-QC sample **1/1**, prelaunch `STOP QC_ENV_NAMES`, D28 two-field and numeric subset are separate snapshots. **Not proven:** historical export-time rejected identities/reasons, all 2,618 accepted QC, same-snapshot numerical/source-field parity, frozen parity, full science/Stage-2 or release. No algorithm/API change in this docs-only task, no DB/network/private/product read, tests, deletion or commit. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-20 M5 TEST source-field boundary — M5_TEST_TWO_SERIALIZED_FIELDS_REPORTED_PASS_2618_SCIENCE_OPEN_PARTIAL

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, 98 grouped dirty entries (75 modified + 23 untracked, including user work), the pinned untracked checker SHA-256 `D28FE34961E9C32A77FD399D525C23DC0F151833F89D5B9081B1F8875DAC1906` and seven unchanged source/test pins matched. Separately approved one-shot guarded READ ONLY on non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`) reported exact whole-shell `PASS M5_TEST_SOURCE_FIELDS_RO accepted=2618 matched=2618 fields=2 scope=TEST_ONLY` and `NATIVE_EXIT=0 WRAPPER_EXIT=0` (native/wrapper/shell exit 0). Fixed private YAML helper read once, one same-backend RR READ ONLY connection, unchanged P207 identity guard, three top-level SELECTs (identity; bounded 18-source/32 current SCIENCE-alias/≤126 normalized-chain metadata; indexed 2,618 SCIENCE/BASE/M4/frame accepted rows with two masked JSON scalars) and rollback/close before comparison are **implied by the pinned checker PASS path/wrapper**, not independently measured/requeried.

The M5 CSV `source_id`/`catalog_name` values come from normalized-chain reconstructed `measurement_flags`, not simple BASE flags. Checker checked BASE JSON string/null/absent types, allowed normalized-stage `catalog_name`/`identity_catalog_name` filters and exact CSV serialized strings across **2,618 accepted five-part keys**: conditional value parity in this **NEW TEST snapshot only**. Previous 2,618-point numeric subset PASS is a **different snapshot**; neither check reran full API reconstruction nor validated survivorship, QC/quality_flags, 22 rejected, export-time snapshot, frozen comparison, whole-science/Stage-2 or release. Earlier prelaunch `ARG_BOUND` STOP, unphased STOP 2/2 (contact unknown), `phase=METADATA` STOP 2/2 (probe reached, cause unknown) and scalar STOP remain dated history, not data-mismatch evidence. Reviewer later checked **offline** HEAD/98, all eight pins, two original regular bundle files (CSV 27,774,120 / JSON 13,952 bytes), staging 0, but did **not** requery DB or rehash product contents. No reported child DB/product/filesystem writes or frozen contact per guards/reports; physical socket closure not independently proven. No algorithm/API change or DB/private YAML/product CSV access, test, deletion or commit by this docs edit. Approval consumed; `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`, `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

## 2026-10-19 M5 opt-in source-head export on non-frozen TEST — bounded PASS, science acceptance OPEN

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10` with **97** pre-existing dirty entries including user work, the source-only M5 patch changes **two** source/test files, not the frozen Stage-2/3 formulas or default behavior. Default M5 still rejects ambiguous alias selection and its budget caps field SCIENCE alias heads at **18**. Explicit `use_explicit_m4_source_heads=True` is a separate human-scientific opt-in: it requires explicit distinct M4 generation IDs, `enforce_export_budget=True`, `preserve_staging_on_failure=True`, `plots=False`, `include_rejected_debug=False`, `allow_partial=False`; its bounded field preflight accepts at most **18 series**, **1–2 current SCIENCE alias heads per series**, **36 total**, validates scope and resolves only exact M4 source heads. The default 18-head cap is **not** an opt-in-wide default change.

Prior bounded metadata check on non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`) found `GPX_TF17_2` **field_id 1**, object **413**, M4 generations **3 V/ref 15** and **4 R/ref 16**, nine series each. Historical frozen-era field_id **33 is not TEST**. There are **18 series / 32 current alias heads** (14 × 2, four × 1), none invalid and all 18 M4 source CVs current alias heads; default M5 failed cap 18. One separately approved, consumed guarded READ ONLY opt-in export of object 413 (child-reported connects/checkouts **1/1**, same-backend RR READ ONLY; native/wrapper exits **0/0**) produced **2,618 accepted** of **2,640 source**, **22 rejected**, V/R **1,309/1,309**, CSV **27,774,120** and JSON **13,952** bytes; no reported staging, deletion or overwrite. Child raw stdout bytes not independently retained; fixed whole-shell PASS only.

Reviewer independent **offline filesystem** postcheck (exit 0, no DB reread) verified just those two bundle files, source/accepted/rejected counts, 18-source/32-alias provenance and finite row-wise `mag_global = mag_science + offset` within **1e-9**. Frozen contact absent per guards/reports; DB writes none by READ ONLY contract, physical socket closure unproven. This is **M5_TEST_BOUNDED_EXPORT_PASS_SCIENCE_ACCEPTANCE_OPEN**, not live TEST scalar-vs-DB, old/new/frozen parity, full science, deletion or release acceptance. All one-shot approvals consumed; this docs edit has no DB contact, source changes, tests, deletion or commit. `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_EXECUTE=NO`.

A later separately approved TEST READ ONLY sample matched **36/36** distinct min/max accepted points across 18 series against persisted SCIENCE/M4 linkage and scalars within **1e-9**; full 2,618-point coverage, flags/QC and frozen parity remain open, with no change to the export algorithm contract.

## 2026-10-16 P2-07 post-migration R4-only READ ONLY — bounded stored-state PASS; OPEN/PARTIAL

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / reported `0.7.0.10`, Reviewer offline postchecked **97 grouped dirty entries** including user work (no per-entry digest), **not the DB**. One newly approved R4-only guarded READ ONLY child on non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`) read the fixed private YAML password once for RO, filtered child-only `PG*`/`GRANDGOUSIER_DATABASE_URL` environment **names**, used NullPool and REPEATABLE READ READ ONLY on the guarded same backend; child physical connects/checkouts **1/1 inferred via fixed PASS**. Unchanged `verify_interseries_generation(session, generation_id=4)` ran **once**; nine exact per-CV SCIENCE counts matched stored `n_science_points` (sum **2,639,909**). Fingerprint/source/graph true, **9 series / 36 OK edges / dof 28**; preflight **36 persisted edges, all OK**, one reference series **16**. Exact child stdout **and whole-shell output** `PASS P207_TEST_R4_SCIENCE_RO n_series=9 n_ok_edges=36 dof=28 points=2639909`; native exit **0**, stderr empty, no retry. PASS required Session close, transaction rollback, Connection close and engine dispose; physical socket closure unproven. Wrapper wall cap **90s**, elapsed unknown; no files by child, DB writes by READ ONLY contract or frozen contact per guarded source/reports; no server-independent DB contact/status check.

Previous post-migration V3 **2,639,629 / 9 / 35 / dof 27** is a **different snapshot**, not a current same-snapshot **5,279,538** sum. Earlier `.7/.8` NEW37/seven-constraint same-child report retains its extra wrapper `System.Threading.Tasks.VoidTaskResult` defect; R4 did not requery the ledger or repair that output. Separate R4 RO approval consumed; no further DB read/write, Stage-2 execute, frozen comparison or release authorized. This proves bounded *stored R4* source-count/fingerprint/graph consistency only, not scalar/formula or old/new/frozen parity, coordinated member/edge or ordered whole-field deletion, full science/Stage-2 or release readiness. Keep the working non-frozen TEST preference. This docs delta has no DB contact/write, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

## 2026-10-16 P2-07 post-migration V3-only READ ONLY — bounded stored-state PASS; OPEN/PARTIAL

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, Reviewer offline postchecked **97 grouped dirty entries** including user work (no per-entry digest), **not the DB**. One separately approved V3-only child read the fixed private YAML password once, filtered child-only `PG*`/`GRANDGOUSIER_DATABASE_URL` environment-variable **names**, and guarded the same physical non-frozen PG18 TEST connection `grandgousier_070_gpx_full` (expected OID `23069087`) under REPEATABLE READ READ ONLY; child-counted connects/checkouts **1/1**. One `verify_interseries_generation(session, generation_id=3)` and nine CV-scoped exact SCIENCE COUNTs matched stored `n_science_points` (sum **2,639,629**). Fingerprint/source/graph true, **9 series / 35 OK edges / dof 27**; bounded preflight **36 stored edges**, one reference series **15**. Child native exit **0**, empty stderr, exact child stdout **and fixed whole-shell output** `PASS P207_TEST_V3_SCIENCE_RO n_series=9 n_ok_edges=35 dof=27 points=2639629`. PASS required Session close, transaction rollback, Connection close and engine dispose; physical socket closure unproven. Wrapper child wall limit **90s**, exact elapsed not reported. No retry, output file/FS write, DB write by READ ONLY contract, or frozen contact per guard/reports.

Earlier `.7/.8` ledger **NEW37**/seven constraints remains *same-child migration report evidence* with its prior `System.Threading.Tasks.VoidTaskResult` whole-wrapper defect; this V3 check neither fixes it nor independently requeries the ledger. The separate RO approval is consumed; no further DB read/write, Stage-2 execute, frozen comparison or release authorized. Only **stored V3 source-count/fingerprint/graph consistency** post-migration is established; not scalar/formula parity, R4 post-migration, whole-field deletion, old/new/frozen or full science acceptance. Continue bounded gate-advancing checks toward working non-frozen TEST only after separate approval. This documentation delta has no DB contact, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

## 2026-10-16 P2-07 TEST application report — bounded NEW37, wrapper output defect; OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, initial dirty status **97** including user work, source manifest **37 / 21 managed**. Per the one explicitly approved guarded inline Python writer report, on **non-frozen PG18 TEST** `grandgousier_070_gpx_full` (expected OID `23069087`), the specially approved fixed private YAML writer credential was used for this invocation only. No verified backup/PITR was accepted; external-writer pause through postcheck was user-attested, not server-proven. One `apply_pending` with only pending `.7/.8` reportedly returned both exact IDs, committing their SQL+ledger in **two separate durable transactions**. A fresh guarded READ ONLY postcheck inside the same child reported **37 ledger rows** (32 BASELINED, `.4/.5/.6/.7/.8` APPLIED) and the seven new expected named constraint shapes:

- `.7` deferred edge→same-generation membership FKs: `fk_interseries_edges_left_generation_member`, `fk_interseries_edges_right_generation_member`.
- `.8` immediate CV composite uniques: `uq_calibration_versions_id_field`, `uq_calibration_versions_id_field_series`; deferred reference→member FK: `fk_interseries_generations_reference_member`; immediate SCIENCE-CV pair/triple FKs: `fk_interseries_series_science_cv_field`, `fk_interseries_series_science_cv_field_series`.

Native child exit **0**, exact stdout `PASS P207_TEST_NEW37_BOUNDED ledger=37 constraints=7`, stderr empty; PowerShell wrapper additionally emitted `System.Threading.Tasks.VoidTaskResult`, violating its fixed-only whole-shell output contract. Reviewer reproduced this type offline from `[Threading.Tasks.Task]::Delay(1).GetAwaiter().GetResult()` (unassigned await plausible, not proven); not child stdout or a reported SQL exception. Executor STOPped for Reviewer audit; Reviewer audited reports and post-Git HEAD/status count (**97**, no per-entry hash), **not an independent DB requery**, and full-wrapper PASS is **not** established. No physical socket-closure proof, SCIENCE-row scan, Stage-2/science/frozen parity or deletion behavior acceptance. Frozen/reference untouched per guarded source/reports only. Earlier source-only `.7/.8` and OLD35 sections below are dated snapshots **superseded at TEST by the reported NEW37**, not erased. `BOUNDED_NEW37_CHILD_POSTCHECK_REPORTED_AND_REVIEWER_AUDITED_WITH_WRAPPER_OUTPUT_DEFECT` is not science/release readiness. This docs edit makes no DB contact/write, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; backup/PITR and coordinated member/edge, ordered whole-field deletion remain open; no further write approved. `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-16 P2-07 source/TEST boundary and operating preference

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / source `0.7.0.10`; initial dirty status **96** including user work. Manifest **37 / 21 managed**, .7/.8 **source-only**. Latest Executor report: ninth OLD35 FK fixed offline in existing guarded integrity helper, **207/207 independent fake tests passed**; live integrity child **STOP exit 2**, stage/DB contact **UNKNOWN**, not DB-drift evidence. Earlier usage-limit attempt **UNKNOWN**, not PASS. A distinct user-approved one-shot guarded ledger, after the dated Oct 11 catalog snapshot but before the integrity-child STOP: bounded non-frozen TEST OLD35 snapshot, exact **35** (32 BASELINED; .4/.5/.6 APPLIED; .7/.8 absent). Ledger-only evidence does not validate live integrity or apply .7/.8.

Prioritize a working non-frozen TEST outcome: avoid new standalone checkers/temp wrappers and non-gate-advancing full hash/test loops. After separate user approval for DB writes, reuse existing under-lock migration preflights and one purposeful minimal postcheck; STOP on material mismatch. Current-session output inspection is permitted; never publish credentials/URLs/private YAML/raw log/env values (user may censor locally). Frozen/reference read-only; no migration/schema/DB write, destructive filesystem/Git, release or secret-handling approval follows from documentation permission. This docs task makes no DB contact/write, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-11 P2-07 boundary — source .8 vs bounded OLD TEST inventory

Source **37 / 21 managed** with .7 SQL mirror SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7` untouched and .8 mirror `2F121E971506A71BB633084982510A88CE70B6FD41D154100E051BDA8E437D61`; runtime requires NOT NULL science CV, .8 preflights all edges under lock. Reviewer **126 focused offline tests passed**, F821/LSP clean; scoped Ruff **NOT clean** (existing F401/I001/SIM118/SIM210), full pytest/PG parser not run. HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3`, dirty status 95 including user work.

One distinct audited outside-temp candidate SHA-256 `CD0EFFABF971AB8703A4F7E2449CF61EAF3F9D7ABF147B8D04882721C30D6676` ran a narrow READ ONLY ledger/catalog inventory on guarded loopback PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`): shell stdout `OLD_LEDGER_CATALOG_INVENTORY_BOUNDED` / `INVENTORY_EXIT=0`, exit 0; post source pins matched (`POST_SOURCE_PINS_MATCHED`, exit 0). It reported old ledger 35 (32 BASELINED; .4/.5/.6 APPLIED; .7/.8 absent), three enforced old edge FK shapes and seven new named constraints absent. Reviewer classified `OLD_BOUNDED_INVENTORY_ONLY` from reports, without DB requery. It did **not** scan CV/member/edge rows, RI-trigger membership, sizes/caps, SCIENCE or frozen parity; older 18/2,640/4/36/144 and fingerprint are historical only. Physical cluster identity/socket closure and writer fencing are unproven. Broad RO verifier preparation STOPped before artifacts; CV/member RO stage remains PLAN-only. No live .7/.8 DDL/application/rollback or write approval. `apply_pending` commits separately; backup/PITR, writer whole-transaction timeout and SET NULL triple-FK deletion behavior remain unknown. This docs update made no DB connection/query or script/SQL invocation. P2-07 OPEN/PARTIAL; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

## 2026-10-11 source-only A2/B .8 boundary

Manifest **37 / 21 managed**; fresh ORM and runtime now require the A2 deferred reference `(generation,reference_series)` → membership `(generation,series)` FK and B immediate membership `(science_cv,field)` and `(science_cv,field,series)` → two immediate CV unique keys. Nullable member `series_id` bypasses only the triple MATCH SIMPLE FK, not the field pair. Existing reference→field-series RESTRICT, generation-member CASCADE, membership series-only SET NULL, ID-only CV RESTRICT and five edge FKs are preserved. Exactly-one `is_reference_series` remains a verifier rule; CV role/lifecycle and publication supersession are not frozen. Writer creates generation before members; deferred A2 resolves at commit. No science formula/selection change. Managed .8 SQL has five ordered ACCESS EXCLUSIVE locks and ledger SHARE with 5s lock/30s statement timeouts; index-build and total transaction costs are not bounded by these settings. Neither .7 nor .8 is permanently applied on TEST: last independent snapshot had ledger 35 (.4/.5/.6 applied) and three edge FKs. Source/runtime therefore currently differ from TEST; ordinary `migrate-db` can commit .7 and .8 separately with PARTIAL-state risk. No real DB connection/query/DDL or verified backup/PITR in this source change; no DB write approval. P2-07 OPEN/PARTIAL; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`.

## Latest P2-07 boundary — 2026-10-10 (A1_V2_RI30_CATALOG_ROLLBACK_ONLY_PASS_BOUNDED_NOT_APPLIED)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, grouped status **93** including user work (digest `997B1784047F84EB242599ACD8852F14680B2218EDBFB8C4497690FD82158006`) matched audited pre/post pins. One NEW current-session user approval covered exactly ONE V2 rollback-only trial of full captured source .7 SQL with TWO ADD CONSTRAINT actions on non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`), accepting no verified backup/PITR for this trial. External writers/schema-changing workflows were attested paused through separate Reviewer Stage2, **not server-proven**; pause requirement completed on Reviewer report delivery to Manager and may be released. Approval **CONSUMED**, frozen reference untouched.

**Stage1 is provisional candidate-reported transient evidence:** Previously audited PowerShell 5.1 common+single-call gate assembly invoked pinned outside-temp V2 RI candidate SHA-256 `4C09DCF1CDABB7BB90B534DA96D647738A0273966FF5AC53A8F0A2F4FCD6FE62` **exactly once**. Sanitized wrapper result exactly `PROVISIONAL_STAGE1_RI30_ROLLBACK_ONLY_STAGE2_PENDING`, wrapper exit **0**; PASS branch requires one fixed candidate PASS/native exit **0 inferred from wrapper, not separately printed**, no extra stderr, and pre/post path/hash/Git pins. Candidate reported one actual execution of captured .7 SQL (two FK additions) and transient **11 outgoing M4 FKs, five edge FKs, four inbound FKs and RI30 shapes**, with five BIGINT casts and forced new-FK checks. Candidate reported explicit outer ROLLBACK and separate internal old-state RO check; no ledger INSERT, commit, DELETE or SCIENCE-row scan. **No independent requery of transient RI30 while DDL existed.** Five edge FKs are the source/fresh-schema target and candidate-reported transient shape, **not** the permanent TEST catalog.

**Stage2 is independent old-state observation only:** Reviewer ran once the pinned outside-temp oldstate-v2 direct-psycopg REPEATABLE READ READ ONLY verifier SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3` in a separate PowerShell process via audited pins. Exact wrapper stdout `PASS_INDEPENDENT_STAGE2_OLDSTATE_BOUNDED`; fixed verifier PASS/native exit **0 inferred from wrapper**, pre/post HEAD/grouped-93 digest pins matched. Its bounded snapshot found old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), old **three** edge FKs, both new named FKs absent across `public`, **22 old RI trigger checks**, exact **18/2,640/4/36/144** series/series-frame/generations/members/all-status edges, zero scoped anomalies, relation caps and fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`. The 2,640 `field_series_frames` were checked, **not writer-fenced**. Reviewer recommendation **`BOUNDED_RI30_ROLLBACK_TRIAL_PASS_NOT_APPLIED`** combines Stage1's candidate-reported transient shape with independent Stage2 old-state only; it is not permanent .7 migration acceptance.

Source .7 manifest **36 / 20 managed**, packaged/scripts raw SQL SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`, source unchanged; TEST remains .7 **not permanently applied** (Stage2 old 35/three edge FKs), managed runner commit not exercised. Earlier 2026-10-10 V1 RI Stage1 STOP/V1 RO STOP, V2 old-catalog JSON RO PASS and distinct 2026-10-09 V3 trial retain their dated scopes. Creator-first timeout/event smoke **UNKNOWN**. No verified backup/PITR, physical-byte equality, proven socket closure or full SCIENCE/old-new/frozen parity; rollback can generate WAL/locks/physical growth. A2/B, manual coordinated member-plus-edge DELETE, ordered whole-field deletion, full science/pytest/release OPEN. This docs-only update makes **no DB connection/query, SQL/DDL/DML execution or script invocation**. P2-07 OPEN/PARTIAL; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

## Previous P2-07 boundary — 2026-10-10 (OLD_CATALOG_JSON_V2_READONLY_PASS_BOUNDED_ONLY)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, grouped status **93** including user work (SHA-256 `997B1784047F84EB242599ACD8852F14680B2218EDBFB8C4497690FD82158006`) was pinned before/after. Manager used the independently audited PowerShell 5.1 wrapper for **exactly one** newly approved V2 OLD catalog JSON READ ONLY script (previously bytes-audited SHA-256 `C8D8BA23B1353788974F0CAF908C4C69ED38CB672F94A53697EE5DFF5E630C22`) on non-frozen PG18 TEST `grandgousier_070_gpx_full`, OID `23069087`. Exact sanitized stdout: `PASS P207_A1_OLD_CATALOG_JSON_V2_READONLY old_fks=9 inbound=2 ri_triggers=22 catalog_json=PARSED ri30=NOT_PROVEN stage1_cause=UNKNOWN`. Wrapper PASS depends on exact child stdout, native exit **0 inferred, not separately printed**, and matching pre/post source/test/SQL/Git/Python hashes. Reviewer `REPORT_AUDIT=PASS_BOUNDED_OLD_JSON_RO` inspected reports, **not a live rerun**. Frozen reference was not contacted; one-shot RO approval is consumed.

The one guarded direct-psycopg REPEATABLE READ READ ONLY snapshot checked target/user/PG18/OID/timeouts, old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), **nine outgoing old M4 FKs / two inbound FKs / 22 RI triggers**, and one corrected V2 `CATALOG` SELECT: five OID::bigint casts, text JSON parsed with strict Python integer checks. This probe did **not** run DDL/DML, SCIENCE or frame-row scans, or an advisory lock. Source .7 manifest **36 / 20 managed** and packaged/scripts SQL mirror SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7` are unchanged; .7 is **not permanently applied**. Separate V2 RI candidate SHA-256 `4C09DCF1CDABB7BB90B534DA96D647738A0273966FF5AC53A8F0A2F4FCD6FE62` is **OFFLINE ONLY**, with four V2 temp fake suites **88 passed combined offline**, no live RI DDL for it.

**RI30 and the new FK pair in the DB are NOT PROVEN by this probe.** The cause or stage of the earlier STOPped V1 RI writer or V1 old-catalog RO probe, full dataset/SCIENCE/physical-byte parity, physical socket closure and managed writer timeout are not established. Preserve the earlier 2026-10-10 Stage1 STOP plus independent Stage2 old-state PASS below and the distinct 2026-10-09 successful V3 rollback-only trial as dated evidence, not transferable acceptance. Creator-first RO timeout smoke is UNKNOWN; no verified backup/PITR, prior RI DDL waiver consumed, **no pause active**. Permanent .7 apply, A2/B, coordinated manual member-plus-edge DELETE, ordered whole-field deletion, full science/old-new/frozen acceptance and release remain OPEN. This docs-only update makes **no DB connection/query or SQL/script invocation**. P2-07 OPEN/PARTIAL; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

## Previous P2-07 boundary — 2026-10-10 (RI_TRIAL_STOP_OLD_STATE_RO_CONFIRMED_NOT_RI30)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / version `0.7.0.10` with grouped status **93** preserved, source .7 is still manifest **36 / 20 managed**, mirrored SQL SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`; **not permanently applied** on non-frozen TEST `grandgousier_070_gpx_full` (PG18, OID `23069087`). The user's exactly-one rollback-only whole-.7-SQL RI catalog trial with **two ADD CONSTRAINT**, accepting no verified backup/PITR, is **CONSUMED**. External writer/schema pause through Reviewer RO postcheck was attested, not server-proven; requirement completed at Stage2 report delivery and may be released. Frozen reference never contacted.

Stage1 ran **once** with pinned RI candidate SHA-256 `ED970F7195722BEC10DD04B51ABED9DF8667A5A559E86FE03B27D51C6D4A9DF9`: audited wrapper **`STOP_RI_TRIAL_NOT_PASS_NO_RETRY`**, candidate fixed STOP line (raw stderr not printed), native exit **1 inferred from wrapper**; pre/post HEAD/grouped-93 status digest pins matched. Whether DB connection, transient DDL or explicit rollback occurred is unknown; **RI30 transient catalog shape NOT ESTABLISHED; NO RETRY**. Separate Reviewer Stage2, exactly **one** pinned old-state-v2 direct-psycopg READ ONLY verifier (SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3`), returned wrapper **`PASS_INDEPENDENT_OLDSTATE_RO_BOUNDED`**, fixed verifier PASS/native exit **0 inferred**, pins matched. At that snapshot, ledger **35** (32 BASELINED, .4/.5/.6 APPLIED, .7 absent), old **three** edge FKs and both new named FKs absent across `public`, old RI **22** and inbound FKs, exact **18/2,640/4/36/144**, zero scoped anomalies, relation caps and fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6` passed. The 2,640 series-frame rows were checked, **not writer-fenced**. This is old logical state at one snapshot, not Stage1 RI30 PASS or proof of transient behavior/byte equality.

The **different** successful V3 SQL-plus-provisional-ledger rollback-only trial below stays dated 2026-10-09; it cannot validate this STOP. Recent creator-first RO timeout/event smoke proposed invocation output is **UNKNOWN after agent usage limit**; no PASS or retry. No verified backup/PITR, full SCIENCE/old-new/frozen parity or physical socket closure; WAL/locks/physical growth possible. Manual coordinated member-plus-edge DELETE, A2/B, ordered whole-field deletion, permanent apply, full pytest/release remain OPEN. This docs-only correction makes **no DB connection/query**. P2-07 OPEN/PARTIAL; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

## Previous P2-07 boundary — 2026-10-09 (A1 .7 rollback-only live trial, not applied)

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / version `0.7.0.10`, source .7 remains in the **36 / 20 managed** manifest (packaged/scripts SQL SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`). One approved V3 Stage1 candidate (SHA-256 `22905165E6196A0217BA5FEB888911BB4043DA257D043F6D7F44D04C0475F609`) reported two transient edge-to-member composite FK DDL statements and a provisional .7 ledger INSERT in one outer writer transaction, **ROLLED BACK**; `SCRIPT_EXIT_CODE=0`, bounded witness unchanged, physical bytes not asserted. Separate internal RO postcheck passed. Independent Reviewer Stage2 direct-psycopg **READ ONLY** verifier (SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3`) ran once and returned `PASS STAGE2_INDEPENDENT_OLDSTATE_READONLY`, exit 0: non-frozen TEST `grandgousier_070_gpx_full` (PG18, OID `23069087`) retains old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), **three** old edge FKs and no new named pair in `public`, exact **18/2,640/4/36/144** series/series-frame/generation/member/all-status-edge counts, 22 RI trigger checks/inbound FKs, zero scoped anomalies, relation caps and bounded fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`. Result **`TRIAL_PASS_ROLLBACK_ONLY_NOT_APPLIED` BOUNDED**; five edge FKs remain the *source/fresh schema target*, not the TEST catalog. The 2,640 `field_series_frames` were checked but not writer-fenced.

The single user approval is consumed; the external pause was attested through Reviewer postcheck and can be released after report delivery, not server-proven. Frozen reference was never contacted. No verified backup/PITR, physical-byte equality, science parity or proven physical socket closure; rollback can create WAL/locks/physical growth. Manual coordinated member-plus-edge deletion, A2/B and ordered whole-field deletion remain untested/open. Full pytest/release not run, P2-07 OPEN/PARTIAL; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`. This docs-only update uses the reports above, not a fresh DB query.

## Current status — 2026-09-28

Package/source version is **0.7.0.10**. The live clean-rebuild target is `grandgousier_070_gpx_full` (OID `23069087`); frozen `grandgousier` remains untouched and read-only. All 18 current Stage-2 heads are `READY_STAGE3_RECIPE`. Current M4 was independently verified as V generation 3 (reference series 15, 9 series, 35 OK edges, dof 27, reduced chi-square 1.4035944978863608) and R generation 4 (reference series 16, 9 series, 36 OK edges, dof 28, reduced chi-square 3.474181644470778). M5 exported 134 unique candidate IDs (234 per-series occurrences); candidate IDs are not scientifically confirmed variables.

Selecting `adaptive_ellipse` does not by itself make a generic Stage-2 run compatible with `stage3_scale_v1`; verify recipe readiness from the persisted provenance. This qualification does not alter the 18 current heads reported above as `READY_STAGE3_RECIPE`.

The live Parquet check covers only series 1/V (CV 127, M4 generation 3) and series 3/R (CV 134, M4 generation 4), 50,000 photometry rows each. Typed schemas, hashes, portable UIDs, M4/global magnitude equation, and recursive removal of internal `catalog_source_id` from measurement flags passed. This does not validate the full 18-series export, archive-scale performance, full test suite, or release acceptance. Stage 1 reprocessing for 2018-09-16 is partial (544 raws downloaded, 186 new reduced products, 358 remaining), has not changed DB/aliases, and was not the source for the existing full Stage-2 SCIENCE used for series 15/16 M4.

Historical P2-07 source-only snapshot (2026-10-09, before the live trial above): A1 .7 edge-to-same-generation-membership
FKs are **source-only, not DB-applied**, at unchanged HEAD / version 0.7.0.10.
Manifest is 36 total / 20 managed; last documented TEST state remains .6 APPLIED,
ledger 35 and three edge FKs, not a new DB query. The frozen reference stays read-only.
Offline focused tests passed (120); live .7 PostgreSQL migration/parser validation,
full science/release acceptance and DB-write approval remain absent. See `CONTEXT.md`
for the current safety gate; the 2026-09-28 results above remain dated evidence.

The architecture details below are retained; older dated status statements are historical snapshots and are superseded by this current scope.

### Historical checkpoint (2026-09-21; superseded)

This checkpoint records the then-current 0.7.0.9 source candidate and 0.7.0.4 test baseline. The frozen 0.6 database remains a reference corpus and is not migrated in place.


## 0. Current 0.7 clean storage/identity/reference-photometry boundary

- `measurements` — только immutable BASE extraction rows; low-level writers проверяют `BASE_PHOTOMETRY / BASE / FULL_MEASUREMENTS`.
- `science_photometry` — durable narrow final science state keyed by `(calibration_version_id, base_measurement_id)` with eight calibration-changing scalars. Publication occurs only after all earlier science/export steps succeed; writer requires `SCIENCE / COMPLETE_IMMUTABLE` and does not retain durable rows for intermediate products.
- Clean-schema science verification compares normalized reconstruction against `science_photometry` + immutable BASE geometry; it must never require the retired `science_measurements` relation.
- Retired 0.6 SCIENCE compatibility relations не входят в fresh `Base.metadata`; их mappings остаются только для frozen-reference forensic code.
- `field_series_frames` — many-to-many membership with uniqueness only on `(series_id, frame_id)`; grouping method participates in series UID.
- `calibration_frame_terms` and `calibration_iteration_objects` derive calibration identity through `calibration_iterations`.
- `init-db` creates/baselines only an empty clean 0.7 schema; `init-db`/`migrate-db` refuse a pre-0.7 compatibility schema before writes.
- Detection storage is run-normalized: `detection_runs` owns algorithm/config/fingerprint/provenance, `detection_run_frames` owns per-frame status/diagnostics, and `source_detections` keeps row-specific source measurements/flags only.
- `detection_catalog_matches` derives frame/catalog/algorithm/config identity through the run/source/catalog graph; `detection_mag_inst`, `catalog_mag`, and `zero_point` remain immutable historical QC snapshots.
- `frame_catalog_links` remains unchanged in Phase B pending a real clean-workload EXPLAIN/bytes benchmark.
- `objects.object_uid` is opaque internal identity; external catalog identities live in explicit `object_catalog_links`.
- Object association is conservative: existing catalog link first, then exactly one epoch/PM-aware candidate within 0.75 arcsec; an Object already linked to the same `catalog_name` is excluded for a different source ID, preserving close same-catalog pairs; ambiguity creates a new Object rather than a false merge.
- `frame_locations` owns mutable locators/file metadata/content fingerprint; `frames` no longer maps `raw_path/file_size_bytes/file_mtime_utc` columns.
- Multiple paths can represent the same Frame; MASTER/RoboPhot native Observation identity remains authoritative.
- Stage-2/Stage-3 production-v1 numerical rules are unchanged.

Phase A–D source work has since been exercised on a fresh 0.7 database with a bounded live GPX rebuild. The current operational gate and its limits are stated above. Sections below describing 0.6.4.5.1 preserve the frozen reference architecture used for old/new comparison.

## 1. Frozen 0.6.4.5.1 reference pipeline

```text
Stage 1: source discovery/materialization/reduction -> processed FITS
Stage 2: immutable BASE extraction + normalized single-series calibration/QC
Stage 3: immutable inter-series generations + global light curves
```

Stage 1 source boundary остаётся source-neutral: `MetadataProvider`, `AssetProvider`, `ProductStore`. P4 physical Observation identity отделена от locator.

MASTER identity = `UNIQNUM`. RoboPhot identity = `SERNUM + DATE-OBS`, причём RoboPhot `DATE-OBS` уже UTC.

## 2. Frozen 0.6.4.5.1 reference database truth layers

### Observation / frame / series

`observations` — authoritative physical-exposure identity.

`frames` в frozen 0.6.4.5.1 — image/product row, который всё ещё содержит `raw_path`. В current 0.7.0.3 source candidate locator вынесен в `frame_locations`.

`field_series` + `field_series_frames` в frozen 0.6 имеют global `UNIQUE(frame_id)`. Current 0.7 source contract уже разрешает альтернативные memberships через uniqueness `(series_id, frame_id)`.

### Object/catalog layer

`objects` в frozen 0.6 использует catalog-dependent `object_uid`. Current 0.7.0.3 source contract использует opaque internal UUID + explicit `object_catalog_links`.

`catalog_sources`, `frame_catalog_links`, `source_detections`, `detection_catalog_matches` — high-volume catalog/detection evidence. В 0.7.0.1 detection rows уже run-normalized, но слой остаётся научно значимым для transient/unusual-variable search и не является cleanup-мусором.

### Photometry/calibration layer

`measurements` сейчас содержит:

- immutable BASE extraction truth;
- 1.8M исторических `FULL_MEASUREMENTS` derived rows.

Current default pipeline уже не создаёт derived full rows: `materialize_intermediate_calibrations=False`, normal derived state = `NORMALIZED_TERMS`.

Normalized state:

```text
calibration_versions
calibration_iterations
calibration_frame_terms
calibration_iteration_objects
```

`reconstruct_calibration_measurement_states()` строит calibration state из BASE + normalized terms.

`CalibrationPhotometryView` — storage-independent reader boundary, но несколько historical/direct SQL readers всё ещё обходят её; это важно при изменении schema.

### Final SCIENCE

Wide `science_measurements` полностью logical-retired и физически truncated до 0 rows.

Current compact state:

```text
science_photometry_cache       10,151,494 rows
science_flag_override_sets            104 rows
science_flag_overrides            917,220 rows
```

Override tables нужны только для exact preservation исторических V1 diagnostic flags. Они не являются желаемой нормой для fresh 0.7 products.

`science_photometry_cache` сейчас формально rebuildable и содержит только:

```text
calibration_version_id
base_measurement_id
mag_cal
mag_err_random
```

Для clean 0.7 он должен стать/быть заменён durable narrow final-science entity с явным научным контрактом.

### Stage 3

Canonical Stage-3 storage:

```text
interseries_calibration_generations
interseries_calibration_series
interseries_calibration_edges
```

`interseries_calibration_offsets` — legacy compatibility storage и не должен определять clean 0.7 модель.

M5 global object export соединяет exact generation-linked Stage-2 science points и не создаёт ещё один full measurement layer.

Для M4 generation reference series принадлежит тому же field; удаление её
`field_series` запрещено (`ON DELETE RESTRICT`). Per-series membership имеет
неизменяемый `field_id` generation: удаление *не-reference* series может установить
только `interseries_calibration_series.series_id = NULL`, сохранив `field_id`.
Такой generation перестаёт быть пригодным для verification/M5/Parquet; reader
отказывает вместо использования частичного графа. Применённая на TEST .6
migration задаёт `ON DELETE RESTRICT` на обоих прямых FK endpoint таблицы
`interseries_calibration_edges` к `field_series.series_id`: любое сохранённое
ребро — независимо от `pair_status` и generation, включая исторические —
блокирует отдельное удаление series. Source-only managed .7 A1 добавляет два
FK от пары `(interseries_generation_id, left/right_series_id)` к membership
`(interseries_generation_id, series_id)` с `ON DELETE/UPDATE NO ACTION
DEFERRABLE INITIALLY DEFERRED`; прямые `RESTRICT` и generation `CASCADE`
сохраняются (в итоге пять edge FKs). Это запрещает orphan-edge даже после
изменения/deletion членства при commit; ORM/fresh schema и runtime contract
ожидают эти пять FK, но **на TEST .7 не применена постоянно**; только
отдельный rollback-only V3 trial с независимой READ ONLY проверкой старого
состояния прошёл 2026-10-09 (previous boundary выше); RI trial 2026-10-10 остановлен.
Для series без ссылающихся edges сохраняется возможность `SET NULL (series_id)`
у membership, если другие FK позволяют. Координированное ручное удаление
member+edge не проверялось; положительный trial охватил только generation
CASCADE. SQL .7 блокирует четыре таблицы ACCESS EXCLUSIVE и ledger SHARE;
последнее может добавить contention/deadlock. Lock timeout 5s и statement
timeout 30s действуют на отдельные statements, не на всю транзакцию.
Нет verified backup/PITR или разрешения на применение; полный field DELETE
требует отдельного явно одобренного упорядоченного workflow, и один
`DELETE FROM fields` не гарантируется. A2 reference-membership и B CV
pair/triple здесь не реализованы; full science/release readiness — NO.

## 3. Database governance

Current schema определяется SQLAlchemy ORM + managed migration ledger `schema_migrations`.

Для действительно fresh DB `grandgousier/db/init_db.py` выполняет:

```text
PostgreSQL >= 15 preflight
-> Base.metadata.create_all
-> assert_schema_contract
-> baseline_current_schema(reason="fresh_orm_init")
```

Следовательно:

```text
fresh install -> init-db
existing managed DB -> migrate-db
legacy pre-ledger adoption -> отдельный validated adopt path
```

Не пытаться создавать fresh 0.7 через replay всей legacy migration history.

## 4. Approved 0.7 clean-schema principles

1. Original FITS остаются вне PostgreSQL.
2. Один immutable BASE photometry row на object/frame/base product.
3. Normal pipeline не materialize-ит full intermediate measurement copies.
4. Final science state durable, narrow и SQL-queryable.
5. Repeated frame/run/calibration context хранится один раз.
6. Catalog identity отделена от physical/internal object identity.
7. Paths/URLs — locators, не identity.
8. Historical 0.6 compatibility tables не должны автоматически входить в fresh 0.7 baseline.
9. Scientific provenance parents immutable; destructive parent deletion не должна silently erase lineage.
10. JSONB — для sparse/raw/rare payload, а не для часто фильтруемых/science scalars и миллионов одинаковых contexts.

## 5. Approved 0.7 structural changes

### A. Core clean schema

`measurements` -> BASE-only; `calibration_version_id NOT NULL`; prohibit normal derived FULL_MEASUREMENTS.

Promote/replace `science_photometry_cache` -> durable `science_photometry` with only scientifically changing final state plus BASE identity reference.

Omit clean-baseline `science_measurements`; historical flag override layer should not be default fresh-schema concept.

`field_series_frames`: remove global unique frame constraint; preserve `(series_id, frame_id)` uniqueness; grouping definition/version explicit.

`calibration_frame_terms` / `calibration_iteration_objects`: calibration identity should be determined through `calibration_iteration_id` or protected by composite integrity; preferred target removes redundant child `calibration_version_id`.

### B. Detection normalization

Implemented source contract in 0.7.0.1:

- `detection_runs` stores run-level algorithm/version/config/fingerprint/pipeline provenance;
- `detection_run_frames` stores frame scope, status and frame-common diagnostics, including zero-result frames;
- `source_detections` stores only row-specific measurements/status/flags plus run-frame FK;
- `detection_catalog_matches` removes duplicated frame/catalog/algorithm/config payload while deliberately retaining historical photometric match snapshots;
- all active readers/writers/QC/event/idempotency/cleanup paths derive context through explicit joins;
- `frame_catalog_links` is not slimmed yet because its query/workload benchmark requires a fresh PostgreSQL smoke dataset.

Physical bytes/row and EXPLAIN are acceptance gates after A–D fresh GPX smoke, before archive-wide rebuild.

### C. Object identity and file locations

Implemented and Windows-real-validated contract in 0.7.0.2:

- `objects.object_uid` is an opaque internal UUID, independent of catalog namespace, source identifier and coordinate formatting;
- explicit `object_catalog_links` records catalog provenance/evidence;
- association is fail-conservative: existing link -> exactly one epoch/proper-motion-aware astrometric candidate within 0.75 arcsec -> otherwise new Object; ambiguous matches are not merged automatically;
- `frame_locations` contains path/URI, size/mtime, availability and optional content SHA-256; these values no longer define Frame/Observation identity;
- a moved/copy-like product can add a second location to the same Frame rather than create another physical observation;
- MASTER `UNIQNUM` and RoboPhot `SERNUM + DATE-OBS(UTC)` remain authoritative physical observation identity.

The policy deliberately prefers occasional duplicate internal Objects over scientifically dangerous false merges. A later explicit association/merge workflow may reconcile such duplicates with stronger evidence.

### D. Reference photometry

Implemented source contract in 0.7.0.3:

- `reference_photometry_catalogs` remains the provider/catalog registry;
- `photometric_bands` defines bands as rows per catalog, so adding a band does not require ALTER TABLE;
- `reference_photometry_sources` stores source identity, astrometry, generic identity-catalog cross-link, colors, source-level quality and one raw provider payload;
- `reference_photometry_points` stores per-source/per-band magnitude, uncertainty and optional point-level quality/provenance;
- importer replacement semantics delete/rewrite a source point set on re-import, preventing stale bands;
- active color calibration, diagnostics, lookup, summary and auto-process readers use normalized data; legacy `mag_*` payload fields are synthesized only at API/calibration boundaries;
- the wide pre-D relation exists only as `LegacyBase` forensic mapping and is forbidden in a fresh 0.7.0.3 schema.

## 6. Tables classification

### KEEP first-class

`schema_migrations`, `observations`, `fields`, `frame_qc`, `calibration_iterations`, `calibration_product_aliases`, `interseries_calibration_generations`, `interseries_calibration_series`, `interseries_calibration_edges`, `artifacts`, `artifact_publications`, batch tables, `event_registry`, `event_object_links`, `event_detection_links`, `processing_events`.

### KEEP but tighten/slim

`instrument_units`, `calibration_versions`, `calibration_frame_terms`, `calibration_iteration_objects`, `catalog_sources`, `frame_catalog_links`, `object_qc`, `field_series`, pipeline provenance/log tables, `reference_photometry_catalogs`.

### REFACTOR/REPLACE before full archive rebuild

`event_frame_coverage` remains a known later cleanup candidate. `objects`, `frames`, `reference_photometry`, `measurements`, `source_detections`, `detection_catalog_matches` and `field_series_frames` have received their clean-schema source refactors but still await fresh-DB physical validation.

### Compatibility-only / omit from fresh 0.7 if readers are cut over

`science_measurements`, `science_flag_override_sets`, `science_flag_overrides`, `interseries_calibration_offsets`.

## 7. Frozen production DB vs 0.7 DB

0.6.4.5.1 DB остаётся reference truth для comparison и historical exact representation. Не пытаться автоматически преобразовать её в 0.7.

Fresh 0.7 DB создаётся только после реализации clean schema в source code. GPX_TF17_2 должен быть повторно обработан из raw/source inputs. Old/new сравнивается по logical identity/science semantics, а не по surrogate numeric IDs.

## 8. Known architecture debts to measure, not guess

- BASE `measurements.flags` JSONB/TOAST может оставаться крупнейшим repeating payload; сначала clean smoke profile, затем factorization experiment.
- high-cardinality detection/catalog tables необходимо сохранять scientifically useful; compression/normalization — только lossless.
- nullable UNIQUE semantics должны быть явными на PostgreSQL 18.
- planner stats старой DB stale для ряда tables; exact counts ≠ `n_live_tup`.
- large pipeline/result JSON лучше хранить в artifacts с compact searchable DB summary.

## 9. Source of truth docs

```text
CONTEXT.md                    current factual state
this file                     architecture contract
ROADMAP.md                    next engineering sequence
PROJECT_HISTORY.md            historical archive
context/project_state.yaml    machine-readable state
```

Legacy `NEXT_CHAT_PROMPT.md` deprecated и не должен заменять canonical context.

## Portable science product: Parquet export

Install the optional dependency with `python -m pip install "grandgousier[parquet]"`.
From PowerShell, set the database URL through the deployment's private configuration
and choose an absolute output directory that does not already exist:

```powershell
$out = "C:\path\to\new\parquet-export"
python -m grandgousier.cli export-science-parquet `
  --database-url $env:GRANDGOUSIER_DATABASE_URL `
  --series-id 1 `
  --out $out `
  --interseries-generation-id 3
```

The command reads through a PostgreSQL READ ONLY transaction, requires explicit series,
and refuses to overwrite an existing output directory. It stages and promotes the
completed directory atomically. Products contain typed `sources.parquet`,
`frames.parquet`, `photometry.parquet`, `series.parquet`, and `manifest.json`; the
manifest contains file hashes and does not contain database credentials. Portable
object/observation/frame UIDs are used instead of database surrogate IDs. Internal
`catalog_source_id` is removed recursively from exported measurement flags.

Stage-3 global values are included only when `--interseries-generation-id N` selects an
immutable M4 generation with a matching SCIENCE calibration for every requested series.
The option is a single generation ID: make separate calls/products for each M4
generation/filter scope. Without it, global fields are marked `NOT_REQUESTED` and the
export is Stage-2-only; nulls are not represented as calibrated global values.

**Live validation is bounded:** series 1/V (CV 127, M4 generation 3) and series 3/R
(CV 134, M4 generation 4), 50,000 photometry rows each, passed schema, manifest-hash,
portable-UID, M4/global magnitude-equation, and internal-ID-removal checks. Full
18-series export, archive-scale performance, the full test suite, and release acceptance
remain unvalidated. Frames are selected independently of photometry, preserving
zero-measurement frames without synthetic science rows. QC category counts may remain
null where persisted classifications are unavailable.
