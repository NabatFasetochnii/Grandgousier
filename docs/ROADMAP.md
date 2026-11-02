# Grandgousier — план перехода к 0.7 clean rebuild
## 2026-10-23 Target-optional resolver correction — IMPLEMENTED; live/science gates remain OPEN
- Current target behavior supersedes earlier target-resolution descriptions for implementation behavior only: after coordinate-pair validation and existing empty-scope / negative-limit checks, a missing object ID, catalog source ID, and paired caller coordinates returns `method="not_requested"` before calibration/frame/header DB queries. MASTER pointing/header values are not target identity; `ra_requested` / `dec_requested` aliases are removed, while pointing/catalog-search aliases and Gaia search-center behavior remain unchanged.
- Existing report fields distinguish `not_requested` / `UNRESOLVED_NO_TRUSTED_TARGET_INPUT` from explicit-but-unresolved selectors. Explicit routes remain. The runner no longer calls the unchanged target-artifact cleanup helper; export still requires a resolved target. Empty target IDs are verified through fake runner/QC tests, including no `FIELD_TARGET` and byte-identical target-named files in pytest `tmp_path`.
- Static reference-membership evidence remains the existing serialized ensemble `reference_object_ids`, persisted `calibration_iteration_objects.is_reference`, and target-LC object/frame/series/calibration identities. Actual live row presence and iteration-to-export joinability remain unverified; no database schema field or migration was added; this patch does add paired RA/Dec runner configuration/CLI inputs and `coordinate_provenance` in resolver output.
- Focused local results: combined field-target/runner/QC/archive-path/Stage-1 command: `112 passed, 1 deselected` (deselected/excluded: real-MASTER inventory test `test_real_master_inventory_exact_gpx_v25_read_only_if_available`). Full pytest and full Ruff were not run. No DB/network/private-config access, real pipeline/archive execution, products, cleanup, schema/formula/materialization change or commit. `READY_FOR_STAGE2_SCIENCE=NO`, `READY_FOR_MASTER_PRODUCTION=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_CLEANUP=NO`, `READY_FOR_COMMIT=NO`.
- **Readiness is field-centric:** no target selector does not itself make field/Stage 1/Stage 2 processing unready; optional target-product readiness is separate. `READY_FOR_STAGE2_SCIENCE=NO` is due to the distinct RoboPhot r ensemble-contamination STOP: object 2275 remained a reference. It is not due to target absence, and no science acceptance is claimed.
- This workflow/identity change did not alter photometry/science formulas or thresholds, or Stage 3 mathematics.
- MASTER `coord2000` remains varying optional archive/search/frame metadata, not target identity; the current parenthesized `coord2000::text` is not parsed by Stage 1, and final semantics/conversion remain open. This is not a global field-science blocker.
- Storage direction: one calibrated field supports multiple downstream object selections; do not rerun full Stage 2 per target. Keep immutable BASE and current validated SCIENCE durable; bound temporary/superseded products. Reduced Stage 1 FITS may be reclaimed only under existing policy after durable Stage 2 validation.
- **NEXT (audit only):** audit one-command bounded processing of the existing complete MASTER archive: MASTER inventory → canonical observation dedup → bounded Stage 1 batches → complete series → Stage 2 → durable science → controlled reclaim → Stage 3/final products. Do not design or implement it in this phase; earlier dated PLAN/NEXT entries below remain historical and are not the current next step.

## 2026-10-22 RoboPhot GPX-TF16E-48 target correction - r ENSEMBLE CONTAMINATED; SCIENCE STOP
- **VALIDATED:** 74 real FITS, 37 i / 37 r, have no requested target cards; `ALPHA/DELTA` varies with scope pointing and `CRVAL1/2` is WCS reference. Old target exports chose i object 2425 / r 2920, 202.78 / 198.03 arcsec from object 2275; mean frame-center offsets 191.12 / 207.25 arcsec. Existing **candidate** CSV for object 2275 / source `531486745299867904`, not corrected FIELD_TARGET: 37/37 accepted in each band, paired Pearson 0.958262, i/r median 14.130775/14.053411 and RMS 0.073063/0.073771 mag. Magnitude vs background -0.924/-0.823, vs x 0.843/0.805; median FWHM 4.3823/4.2749 px, random errors 0.003985/0.004824 mag, local backgrounds 1517/1917.5; no systematic-error CSV values. Correlations can be time/trend-confounded; no causality or variability accepted.
- **VALIDATED separate TEST read-only gates:** guarded same-backend non-frozen `grandgousier_070_gpx_full` OID `23069087`: 74 scoped frame identities, exact link to object 2275 (object/catalog epoch 2016, 0.000000 arcsec), one nearby candidate within 2 arcsec; two BASE flag samples and separate bounded 74 BASE rows with consistent source and `catalog_source_id=50282`. Gaia source code queries DR3 but `gpt.txt` labels ID DR2: release equivalence OPEN. Historical 5000 MAXREC warning and prior i/r candidate triage raw 176/169, RMS-excess 166/157, intrinsic 51/53, low amplitude 5/12 are not current completeness/acceptance gates.
- **IMPLEMENTED SOURCE ONLY:** paired explicit target RA/Dec in degrees, unique measured target within 2 arcsec or scoped unique catalog link, provenance/conflict and mixed/unknown-frame refusal, no nearest-center authoritative target for RoboPhot **or MASTER** (`lightcurves/target.py`, `pipeline/runner.py`, `archive/auto_process.py`, `cli.py`, two focused test modules). MASTER archive DB lookup not implemented; explicit ID/coords supported. No migration/schema or formula change, no live rerun, DB write or product overwrite. Executor reports 75 focused tests (possibly other tests), Reviewer independently 68 PASS in two suites, F821/diff --check PASS, full Ruff **57 findings, individual pre-patch attribution not revalidated**; full pytest/doctor NOT RUN. Previous top STOP in other canonical docs is superseded **for source patch only**.
- **NEW separate Reviewer guarded non-frozen TEST RR READ ONLY gates:** object-QC on final i SCIENCE CV214/unit3, r CV218/unit4 reports object 2275 37 points/filter, median 14.130775/14.053411, sample RMS .073063/.073771 mag; nearest neighbor 31.55 arcsec; QC candidate controls 2298/2305 each report 37/filter, isolated >10 arcsec. Initial bounded SCIENCE-control read **STOPped at i** control 2284: 36 stored SCIENCE vs QC n37. Cause OPEN, not proof of target mismatch/intrinsic failure. Separate three-object (2275,2298,2305) × 37 frames/filter SCIENCE check: 111 rows/filter, target `mag_cal` vs existing candidate CSV maxabs 0; controls 2298/2305 37/37 each. i controls RMS .0090/.0087 mag and corr target .279/-.183; r RMS .0107/.0125 and corr -.116/.132. Two mean-centered controls subtracted: target RMS .07277 i / .07405 r vs raw .07306/.07377. This is provisional stored-science control evidence, NOT accepted-science QC, matched-background/position/color causality, detection support or independent intrinsic-variability proof.
- **NEW separate bounded guarded TEST CV/source-chain check:** final SCIENCE CV214 i / CV218 r; ENSEMBLE iterations i182 / r185. i requested and actually excluded wrong **2425** (`EXCLUDED_REFERENCE`, false); 2275 `NON_REFERENCE`, false. r requested wrong **2920**, actually excluded `[]` (2920 not a reference, `REQUESTED_EXCLUSION`, false); **2275 `REFERENCE`, true**. `runner.py:1728` feeds resolved target IDs into `exclude_reference_object_ids`. r target-reference membership contamination DEMONSTRATED, photometric bias still UNKNOWN. A corrected r target CSV by itself cannot correct ensemble/downstream r SCIENCE. i 2275 was not a reference, but i exclusion of 2425 may have changed calibration; validity/invalidity of stored i SCIENCE unproven. Both old FIELD_TARGET assignments wrong.
- **NEXT, PLAN ONLY (not execution authority):** For exact i-series **19** / r-series **20**, audit old run IDs/source chains, 37-frame memberships and old products; resolve i 2425 effect and 2284 36/37 gap; establish Gaia release provenance, accepted-science control QC, background/position/seeing/error, detection, zero-point and calibration gates. Evaluate r fresh-name ensemble recompute from existing upstream BASE/normalized measurements with explicit 2275 exclusion and scoped downstream publication, only if provenance/safety can be shown. Public `ensemble-calibrate` has no exclusion flag; `process-archive-path` plan-only does not compute a corrected ensemble; execution can write DB, unlink old products if unresolved, or overwrite if resolved. Preserve old artifacts; require Reviewer audit and separate explicit current-session user approval before any DB/FS write or pipeline. No blanket Stage-2 rerun, re-export, deletion or release authorization. This docs-only edit has no DB contact/write, tests, product writes/deletions or commit. `READY_FOR_STAGE2_SCIENCE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXPORT_OVERWRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_COMMIT=NO`, `READY_FOR_RELEASE=NO`.

### 2026-10-21 M5 gate - PRIVATE_SOURCE_STOP / further READ gate closed
- No new worktree: `master` HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, grouped 98 (75 tracked/23 untracked groups), staged 0; existing `agent/luna-test` HEAD `0648cea58da83d777be0b2231cec65b8c7d5ea2e`, grouped 18 (8/10), staged 0; separate history, no copy/merge.
- Expanded `-uall` dated reads ~1.1k (1109/1112/1115); most untracked were `.opencode` (Reviewer saw 1012 then 1015). Exact change cause UNKNOWN; grouped 98 is the baseline, not an expanded-count invariant.
- Untracked checker `tools/check_m5_test_source_fields.py` SHA-256 `1024EA99F4396438AD378E6EC64008BD8EDABBEB993D7BF6D374C1DCC8EC7644`; historical B747→3365 provenance UNKNOWN; two later approved offline checker fixes reported, origin not inferred; seven selected source/test pins matched offline.
- Latest exact `STOP M5_TEST_CANDIDATE_PLAN_SIZE_RO stage=PRIVATE_SOURCE code=UNEXPECTED`, exit 2; pinned path/report put STOP before DB connection: 0 SELECT/EXPLAIN, no size/new plan. Private-source read and exact cause UNKNOWN.
- Next gate PLAN ONLY: size/access-path audit requires new approval before candidate/term SELECT. Earlier `field_series_frames SEQ_SCAN` is a planner access-path STOP, not science mismatch; export 2640 = 2618 accepted + 22 rejected, QC22/numeric/source distinct TEST snapshots.
- Docs only, no DB/frozen contact. Further DB read needs new plan → Reviewer audit → scoped user approval; DB write/GPX execute/frozen write/release/commit NO. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-20 M5 gate: FRAME_TERM_CARDINALITY_OPEN_BLOCKED; PLANNER_ACCESS_PATH_STOP

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`; pre-edit Git **98** dirty (75 modified + 23 untracked incl. user work), staging **0**; untracked checker SHA-256 `B74798D32F7CFA221E9148AD975510C1099C3FBE68BBC270D503C7D43AD088CA` after approved checker-only edits and offline fakes/static guards; seven source/test pins matched. Earlier candidate-key PASS at `7439604C890B0919EF10F12046C9F09730EB52331C5D14022A5C7143501F6DB3` and QC22 22+1 selected object-local/histogram PASS at `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674` remain **separate dated TEST snapshots**: no export-time rejected identities/full accepted science or new live combined PASS.

**Science policy decision:** user explicitly chose KEEP. `measurement EDGE=False` + `object EDGE=True` → merged `EDGE=False`, `OBJECT_EDGE=True`; default EDGE reject ignores OBJECT_EDGE; two-case inline synthetic demo confirmed behavior; no science-policy code change. One approved OFFLINE no-follow read of the fixed original accepted CSV (**27,774,120 bytes**, **2,618 accepted**, V/R **1,309** each) reported `EDGE`, `OBJECT_EDGE`, `FRAME_EDGE` keys absent for **every accepted row only**, not proven absent from 22 omitted rows/current TEST/other objects. No product rewrite or output IDs published; raw log unnecessary/not opened.

**Three separately approved/CONSUMED one-shot guarded READ ONLY diagnostics** on non-frozen PG18 TEST `grandgousier_070_gpx_full` expected OID `23069087`, distinct snapshots: (1) `STOP M5_TEST_FRAME_TERM_CARDINALITY_RO phase=PLAN` then `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2; four-EXPLAIN stage/count reached **UNKNOWN**, no term-cardinality result; Executor offline postcheck `$Matches` collision exit 2. (2) `STOP M5_TEST_FRAME_TERM_PLAN_RO stage=CANDIDATE code=LARGE_SCAN_KIND` then `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2; pinned path **implies four bounded SELECTs + three non-ANALYZE EXPLAINs**, no candidate/term-row SELECT. (3) latest `STOP M5_TEST_CANDIDATE_PLAN_SHAPE_RO stage=CANDIDATE code=LARGE_SCAN_KIND relation=field_series_frames scan=SEQ_SCAN` then `NATIVE_EXIT=2 WRAPPER_EXIT=2`, shell 2; pinned path **implies four bounded SELECTs + one non-ANALYZE EXPLAIN**, no candidate/term-row SELECT. No raw plan published; no independent server audit of path implications. Reviewer independently **offline after latest STOP** confirmed HEAD/98, staging 0, checker SHA and seven pins PASS. These are planner **access-path** STOPs, **not** ambiguous terms, DB drift, science mismatch or proof ambiguity is absent.

**Next PLAN only, not execution permission:** schema unique `(calibration_iteration_id,frame_id,term_kind)` versus frame-alone normalized reconstruction indexing; QC22 addressed selected **23 pairs**, not frame-term cardinality. `LIMIT 2641` bounds output, **not join work**. Future exact `pg_relation_size` + structural EXPLAIN screening of whether a `field_series_frames` sequential scan is safe is **UNEXECUTED**; it does not automatically authorize candidate/term-row SELECT. All checker edits, child PG-name filtering, private-YAML and DB one-shots consumed. Require **new plan → Reviewer audit → scoped user approval** before any new execution/file overwrite. No DB writes by READ ONLY contract; no frozen contact per guards/reports (not server-independently verified), socket closure unproven, no reported product/output writes. This docs task has no DB/network/private/product/log read, code edit, science test, deletion or commit. Frozen/release/full accepted QC/full iteration/COLOR still open. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-20 M5 gate: CURRENT_TEST_OBJECT_LOCAL_QC22_REPORTED_PASS; SCIENCE_ACCEPTANCE=OPEN_PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`; grouped dirty state **98** (75 modified + 23 untracked, including user work). The untracked checker `tools/check_m5_test_source_fields.py` is now SHA-256 `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674` (**127,093 bytes**) with seven unchanged source/test pins; one-file edit approval consumed. Do not overwrite the historical D28/two-field gate below.

**Distinct earlier candidate gate:** at checker SHA-256 `7439604C890B0919EF10F12046C9F09730EB52331C5D14022A5C7143501F6DB3`, one separately approved READ ONLY TEST child returned whole-shell `PASS M5_TEST_CANDIDATE_KEYS_RO candidates=2640 accepted=2618 missing=22 scope=CURRENT_TEST_CANDIDATES_ONLY` then `NATIVE_EXIT=0 WRAPPER_EXIT=0`, native/wrapper/shell **0/0/0**. Its PASS path implies a guarded same-backend RR READ ONLY P207 PG18 non-frozen TEST `grandgousier_070_gpx_full` (expected OID `23069087`) connection, three SELECTs and rollback/close; it did **not** establish normalized survivorship, historical rejected keys or reasons. Approval consumed.

**New independently approved QC22 gate:** at checker SHA-256 `531EEF3205A97CCB08426C6788534F530FC3A39A575C5147FB5F422651395674`, exact whole-shell `PASS M5_TEST_CURRENT_CANDIDATE_QC_RO missing=22 control=1 scope=CURRENT_TEST_OBJECT_LOCAL_QC_ONLY` then `NATIVE_EXIT=0 WRAPPER_EXIT=0`, native/wrapper/shell **0/0/0**. Code PASS path implies one original bundle read, one fixed P207 private YAML read, one guarded same-backend RR READ ONLY TEST session with **eight top-level SELECTs** (probe, old 18-source metadata, 2,640 candidates, 23 exact points, ≤126 chain nodes, two selected bounded term-pair queries, ≤23 object-QC requests). Recomputed **22 current** missing keys survived selected object-local normalized stages, were rejected under current QC and matched **global plus per-series** reason histograms in the fixed export summary; one accepted CSV control matched its three serialized QC fields. Multiple reasons per row are possible; do not equate total reason occurrences with 22. Rollback/close before in-memory comparison per PASS path.

Reviewer audited checker source and independently checked **offline after QC22 PASS** HEAD/98, checker plus seven pins, two regular fixed original bundle files (CSV **27,774,120**, JSON **13,952** bytes); **no independent DB requery or product-content rescan** and no independent socket-closure proof. No frozen contact per guards/reports (not server-verified); no DB writes by READ ONLY contract or reported product writes/deletions. The earlier one-accepted QC sample **1/1**, prelaunch `STOP QC_ENV_NAMES`, D28 two-field and numeric subset remain separate dated snapshots. **Next gates still need separate plan, Reviewer audit and user approval:** historical export-time rejected identities/reasons, full-iteration/other-object COLOR completeness, all 2,618 accepted QC, coordinated same-snapshot numeric/source-field parity, frozen/whole-science/Stage-2 or release. All checker edit, environment-name filter and private YAML/DB permissions are consumed. This docs task reads no DB/network/private/product contents, runs no tests, deletes nothing and creates no commit. `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-20 M5 gate: M5_TEST_TWO_SERIALIZED_FIELDS_REPORTED_PASS_2618_SCIENCE_OPEN_PARTIAL
At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, initial grouped status **98** (75 modified + 23 untracked including user work), the untracked checker SHA-256 `D28FE34961E9C32A77FD399D525C23DC0F151833F89D5B9081B1F8875DAC1906` and seven unchanged source/test pins matched. Exactly one separately approved guarded REPEATABLE READ READ ONLY TEST check, now **consumed**, returned whole-shell `PASS M5_TEST_SOURCE_FIELDS_RO accepted=2618 matched=2618 fields=2 scope=TEST_ONLY` then `NATIVE_EXIT=0 WRAPPER_EXIT=0` (native/wrapper/shell exit 0). Non-frozen PG18 `grandgousier_070_gpx_full`, expected OID `23069087`; fixed private YAML read once, unchanged P207 identity probe, bounded 18-source/32 SCIENCE-alias/≤126-chain metadata, indexed accepted SCIENCE/BASE/M4/frame two-field SELECT, one same-backend connection, rollback/close before Python comparison: **implied by pinned checker PASS path/wrapper, not independently requeried**.

**Gate advanced only for conditional `source_id`/`catalog_name` serialized value parity across 2,618 accepted five-part keys in this NEW TEST snapshot.** CSV fields derive from reconstructed flags; checker checks BASE JSON string/null/absent types, allowed normalized-stage catalog filters and exact serialized strings, not full API reconstruction, survivorship, QC/quality_flags, the 22 rejected, export-time snapshot or frozen/whole-science/Stage-2/release parity. Previous full 2,618 numeric-subset PASS was a **different snapshot**, not a combined acceptance. Earlier prelaunch `ARG_BOUND` STOP, unphased STOP 2/2 (contact unknown), `phase=METADATA` STOP 2/2 (probe reached, cause unknown) and scalar STOP stay dated history. Reviewer independently checked **offline after PASS** HEAD/98 and eight pins, two original regular bundle files (CSV 27,774,120 / JSON 13,952 bytes), staging 0; **no DB requery or product-content rehash**. No reported child writes; no frozen contact per guards/reports, physical socket closure unproven. **Next:** separate plan/approval for remaining acceptance; this docs edit has no DB/private YAML/product CSV access, tests, deletion or commit. `SCIENCE_ACCEPTANCE=OPEN_PARTIAL`; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

### 2026-10-19 M5 gate: M5_TEST_FULL_SCALAR_NUMERIC_REPORTED_PASS_SOURCE_LINKAGE_STOP_OPEN_PARTIAL
At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, 97 pre-existing dirty entries including user work and unchanged source/test SHA-256 pins, the prior full2618 STOP at zero comparisons remains historical. One **separately approved, now consumed** corrected guarded same-backend REPEATABLE READ READ ONLY check on non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`) used fixed private YAML once; static alias and cursor-description guards passed. One successful connect/three SELECTs (identity, same-snapshot 18-source/32-alias metadata, 2,618 science keys); rollback/close invoked before in-memory comparison.
Whole-shell PASS prefix (18 per-series counts omitted here): `PASS M5_TEST_FULL_SCALAR_RO connects=1 selects=1/1/1 metadata=18/32 accepted=2618 matched=2618 g3=1309/9 g4=1309/9`; exact native/wrapper exit line: `NATIVE_EXIT=0 WRAPPER_EXIT=0`. Child reported 2,618 unique ordinal completions, V/R 1,309 each over nine series; the 20 numeric fields were checked NULL-aware finite abs <=1e-9 along with BASE/CV/frame/M4/filter/reference/graph/series membership and `date_obs` UTC microseconds. **2,618 matched is the implemented numeric/link subset only**: CSV `source_id` and `catalog_name` were NOT compared to persisted/reconstructed flags; unqualified source-linkage PASS is revoked. CSV names derive from `measurement_flags` in `lightcurves/product.py`; `photometry/calibration_view.py` reconstructs these flags via normalized chain before SCIENCE scalar overlay, not simple BASE flags.
Reviewer offline postcheck **after** new child matched HEAD/97/pins, exactly two bundle files (CSV 27,774,120 / JSON 13,952 bytes, staging 0), all 18 CSV per-series counts summing 2,618; **no independent DB requery**. Earlier export and 36/36 sample PASS remain dated. **Next gate:** source identity/flags including 22 rejected, export-time snapshot, frozen parity, full science/Stage-2 and release remain OPEN. New full-source/flags check requires NEW plan → Reviewer audit → fresh user approval; consumed permission is not a retry gate. No DB write by READ ONLY contract, frozen contact per guards/reports, product write, or proven physical socket closure; this docs edit has no DB contact, tests, deletion or commit. `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

### 2026-10-19 M5 gate: FULL_TEST_SCALAR_STOP_BEFORE_COMPARE; OPEN/PARTIAL
One separately approved, now **consumed**, guarded same-backend RR READ ONLY check on non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`) used the fixed two-file M5 bundle (CSV 27,774,120 / JSON 13,952 bytes). One connect; SELECT1 identity, SELECT2 same-snapshot 18-source/32-alias metadata PASS, SELECT3 bounded typed-array PK returned 2,618 expected-cardinality rows; explicit rollback/close BEFORE comparison. Exact whole-shell: `STOP M5_TEST_FULL_SCALAR_RO phase=COMPARE code=UNEXPECTED connects=1 selects=1/1/1 matched=0` then `NATIVE_EXIT=2 WRAPPER_EXIT=2`; **matched=0 means zero compared, not a scientific mismatch**. No retry/output file, DB write by READ ONLY contract or frozen contact per guards/reports.
Offline Executor audit found `graph_status` projected but missing `gen_graph` accessed in comparator (unconditional lookup failure if reached); consistent with STOP, not proven sole runtime cause. Reviewer offline postcheck AFTER STOP matched HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, 97 dirty entries incl. user work, source/test pins, exactly two bundle files/sizes, own staging 0; no DB requery. Earlier distinct export PASS and 36/36 sampled TEST scalar PASS remain dated; all 2,618 scalars, flags/22 rejected, export-time snapshot, frozen parity, full science/Stage-2 and release OPEN. No DB contact, code/test edit, tests, deletion or commit in this docs update. **Next:** NEW plan → Reviewer audit → user approval; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

### 2026-10-19 M5 gate: M5_TEST_BOUNDED_SCALAR_SAMPLE_PASS_SCIENCE_ACCEPTANCE_OPEN

Following the distinct bounded opt-in export below, one separately approved, now **consumed** guarded RR READ ONLY check on non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`) compared **36 distinct min/max accepted rows**, two per each of 18 series, with persisted SCIENCE/M4 linkage, calibration/error/offset scalars and `mag_global = s.mag_cal + offset`, null-aware finite absolute **1e-9**. **36/36** sampled matches: generation 3 **18/nine V series**, generation 4 **18/nine R series**. Exact whole-shell lines: `PASS M5_TEST_SCALAR_RO connects=1 probe=1 science=1 samples=36 matched=36 g3=18/9 g4=18/9` and `NATIVE_EXIT=0 WRAPPER_EXIT=0`. Native exit 0/empty stderr required; wrapper compared trimmed child stdout, not preserved raw bytes. One successful connect, one identity probe and one SCIENCE/M4 SELECT on the same backend were child-counted; fixed private YAML read once, no values recorded. No DB writes by READ ONLY contract or frozen contact per guards/reports; physical socket creation/closure unproven.

**Next goal (separate approval required):** complete TEST scalar coverage beyond the 36-point sample (not all **2,618** accepted), then separately address flags/QC, frozen parity, full science/Stage-2, deletion behavior and release. Reviewer independently checked **offline only** unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, 97 dirty entries including user work, M5 source/test pins and two pre-existing bundle files at CSV **27,774,120** / JSON **13,952** bytes; its own stages exited **0**, **no independent TEST DB requery**. No new product, overwrite/deletion, source/test edit, tests, DB contact in this docs edit or commit. M5/P2-07 **OPEN/PARTIAL**; `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_RELEASE=NO`.

### 2026-10-19 M5 gate: M5_TEST_BOUNDED_EXPORT_PASS_SCIENCE_ACCEPTANCE_OPEN

Unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`; **97** pre-existing dirty entries including user work, independently matched source/test pins. Opt-in-only M5 two-file source patch preserves default refusal and Stage-2/3 formulas; focused fake suite **55 passed / 53 deselected**, Reviewer independent **4 selected PASS**, scoped Ruff **5 known pre-existing findings**. No tests in this docs task.

Prior non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`) metadata: `GPX_TF17_2` **field_id 1**, not historical frozen-era 33; object **413**, V3/ref15 and R4/ref16, 18 series and **32 current aliases** (14 × 2, four × 1), no invalid; 18 M4 source CVs are alias heads. Default M5 failed cap **18** and still refuses ambiguity. One separately approved, now consumed, fixed-private-YAML-once guarded READ ONLY opt-in export with explicit M4 heads and enforced budget (field 1/object 413/gens [3,4]; staging preserved on failure; plots/rejected/debug/partial off) reported **1/1** connects/checkouts; same-backend RR READ ONLY, native/wrapper exits **0/0**, fixed full-shell PASS. Bundle present under approved pre-existing user Temp/opencode: CSV **27,774,120 bytes**, JSON **13,952 bytes**; source **2,640 = 2,618 accepted + 22 rejected**, V/R **1,309/1,309**, staging prefix **0**, no reported overwrite/deletion. Child raw stdout bytes not independently preserved.

Reviewer independent **OFFLINE filesystem** postcheck (exit **0**, no DB reread) verified only two files, counts and 18-source/32-alias provenance, per-row linkage and finite global-magnitude equation within **1e-9**. No frozen contact per guards/reports; DB writes none by READ ONLY contract; physical socket closure unproven. **Next acceptance remains separate:** live TEST scalar-vs-DB, old/new/frozen parity, full science/Stage-2 acceptance, deletion semantics and release are still OPEN/PARTIAL. Obtain fresh explicit user approval before any DB read/write, consequential execute, or release; consumed one-shot YAML/DB/export permissions grant none. No DB contact, tests, source change, deletion or commit in this docs update. `READY_FOR_FURTHER_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_EXECUTE=NO`.

### 2026-10-16 P2-07 gate: POST_MIGRATION_R4_SCIENCE_RO_BOUNDED_PASS; OPEN/PARTIAL

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / reported `0.7.0.10`, Reviewer offline postchecked **97 grouped dirty entries** including user work (no per-entry digest), **no DB rerun**. One newly approved R4-only guarded READ ONLY child on non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`) read the fixed private YAML password once for RO, filtered child-only `PG*`/`GRANDGOUSIER_DATABASE_URL` environment **names**, used NullPool and REPEATABLE READ READ ONLY on the guarded same backend; child physical connects/checkouts **1/1 inferred via fixed PASS**. Unchanged `verify_interseries_generation(session, generation_id=4)` ran **once** and nine exact per-CV SCIENCE counts matched stored `n_science_points` (**2,639,909** total). Fingerprint/source/graph true; **9 series / 36 OK edges / dof 28**; bounded preflight **36 persisted edges, all OK**, one reference series **16**. Exact child stdout **and whole-shell output** `PASS P207_TEST_R4_SCIENCE_RO n_series=9 n_ok_edges=36 dof=28 points=2639909`; native exit **0**, stderr empty, no retry. PASS required Session close, transaction rollback, Connection close and engine dispose (physical socket closure unproven). Wrapper wall cap **90s**, elapsed unknown; no files by child, DB writes by READ ONLY contract or frozen contact per guarded source/reports; no server-independent DB contact/status check.

**Next gates remain separate:** prior post-migration V3 **2,639,629 / 9 / 35 / dof 27** was a **different snapshot**, not a current same-snapshot **5,279,538**. Earlier `.7/.8` NEW37/seven-constraint postcheck still has its extra wrapper `System.Threading.Tasks.VoidTaskResult` defect; R4 neither repaired it nor requeried the ledger. R4 establishes only bounded *stored* source-count/fingerprint/graph consistency, not scalar/formula or old/new/frozen parity, coordinated member/edge deletion, ordered whole-field deletion, full science/Stage-2 or release readiness. Its separate RO approval consumed; no further DB read/write, Stage-2 execute, frozen comparison or release authorized. Continue working non-frozen TEST preference, require separate approval for any next gate. This docs-only update makes no DB contact/write, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_EXECUTE=NO`.

### 2026-10-16 P2-07 gate: POST_MIGRATION_V3_SCIENCE_RO_BOUNDED_PASS; OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10` unchanged; Reviewer independently offline postchecked **97 grouped dirty entries** with user work, no per-entry digest or DB rerun. One separately approved READ ONLY V3 child read the fixed private YAML password once, filtered child-only `PG*`/`GRANDGOUSIER_DATABASE_URL` environment names, guarded the **same physical connection** to non-frozen PG18 TEST `grandgousier_070_gpx_full` (expected OID `23069087`), REPEATABLE READ READ ONLY; child counted **1** physical connect/**1** checkout. One `verify_interseries_generation(session, generation_id=3)` plus **nine** exact CV-scoped SCIENCE COUNTs matched stored `n_science_points` (**2,639,629** total). Fingerprint/source/graph true; **9 series, 35 OK edges, dof 27**; bounded preflight **36 stored edges**, one reference series **15**. Native child exit **0**, empty stderr, exact child stdout **and fixed whole-shell output** `PASS P207_TEST_V3_SCIENCE_RO n_series=9 n_ok_edges=35 dof=27 points=2639629`. PASS required Session close, rollback, Connection close and engine dispose (physical socket closure unproven). Wrapper child wall limit **90s**, exact elapsed not reported; no retry, output file/FS write, DB write by READ ONLY contract, or frozen DB contact per fixed guard/reports.

**Next gate remains separate:** earlier `.7/.8` NEW37/seven-constraint postcheck is same-child migration report evidence with its **prior extra wrapper `System.Threading.Tasks.VoidTaskResult`**, not repaired here and not independently ledger-requeried. This V3 run proves bounded *stored* source-count/fingerprint/graph consistency **only**, not scalar/formula parity, post-migration R4, ordered whole-field deletion, old/new/frozen acceptance or full science readiness. RO approval consumed: no more DB read/write, Stage-2 execute, frozen comparison or release authorized. Keep working-TEST and bounded gate-advancing preference without new standalone wrappers or repeated full loops; obtain separate approval for any next gate. This docs-only update makes no DB contact, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_READ=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_RELEASE=NO`, `READY_FOR_EXECUTE=NO`.

### 2026-10-16 P2-07 gate: BOUNDED_NEW37_CHILD_POSTCHECK_REPORTED_AND_REVIEWER_AUDITED_WITH_WRAPPER_OUTPUT_DEFECT; OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, initial dirty status **97** including user work, source manifest **37 / 21 managed**. One approved guarded inline Python writer on **non-frozen PG18 TEST** `grandgousier_070_gpx_full` (expected OID `23069087`), with the specially approved fixed private YAML writer credential for this one invocation, reportedly called `apply_pending` once with exactly pending `.7/.8`, returned both exact IDs and committed **two separate durable SQL-plus-ledger transactions**. No verified backup/PITR was accepted; pause of external writers through postcheck was user-attested, not server-proven. Fresh guarded READ ONLY postcheck *inside the same child* reported ledger **37** (32 BASELINED, `.4/.5/.6/.7/.8` APPLIED) and seven expected named constraint shapes: `.7` `fk_interseries_edges_left_generation_member`, `fk_interseries_edges_right_generation_member`; `.8` `uq_calibration_versions_id_field`, `uq_calibration_versions_id_field_series`, `fk_interseries_generations_reference_member`, `fk_interseries_series_science_cv_field`, `fk_interseries_series_science_cv_field_series`.

Child native exit **0**, exact stdout `PASS P207_TEST_NEW37_BOUNDED ledger=37 constraints=7`, stderr empty. **Not a strict whole-shell PASS:** wrapper also emitted `System.Threading.Tasks.VoidTaskResult`, violating fixed-only classification. Reviewer reproduced the type offline via `[Threading.Tasks.Task]::Delay(1).GetAwaiter().GetResult()`; an unassigned await is plausible, not proven; this was not child stdout or a reported SQL exception. Executor STOPped for Reviewer audit; Reviewer audited reports and post-Git HEAD/status count (**97**, no per-entry hash), **no independent DB requery** or physical socket-closure proof. Frozen untouched per guarded source/reports only. Earlier OLD35/source-only gate entries below are dated and **superseded on TEST by the reported NEW37**, not deleted.

**Next gates:** no further DB write approved; no SCIENCE scan or full Stage-2/science/frozen parity acceptance. Verify remaining coordinated member/edge deletion and ordered whole-field deletion semantics only with separately approved gates; do not assume readiness for Stage-2 execute, archive/release or further migration. No verified backup/PITR, pause not server-proven; preserve the user's preference for a working non-frozen TEST and bounded gate-advancing checks rather than standalone checkers or repeated full loops. This docs-only update makes no DB contact/write, code change, test, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_STAGE2_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-16 P2-07 next gate: WORKING_NONFROZEN_TEST_PRIORITY; OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, initial dirty status **96** with user work; source manifest **37 / 21 managed**, .7/.8 source-only. Latest Executor report: ninth OLD35 FK corrected offline in existing integrity helper, **207/207 independent fake tests passed**; live integrity child **STOP exit 2**, stage/DB contact **UNKNOWN**, not proven DB drift. Earlier usage-limit attempt **UNKNOWN**. A separate approved one-shot guarded ledger, after the dated Oct 11 catalog snapshot but before the integrity-child STOP, returned OLD35 at a bounded non-frozen TEST snapshot: exact **35** (32 BASELINED, .4/.5/.6 APPLIED; .7/.8 absent). Ledger result is not live integrity or migration acceptance.

Next: favor a working non-frozen TEST result, not new standalone checkers/temp wrappers or non-gate-advancing full hash/test loops. Request separate explicit current-session approval before any DB write/migration/schema change; after approval use existing under-lock migration preflights and one purposeful minimal postcheck, STOP on material mismatch. Destructive filesystem/Git, release, and secret-handling changes also require separate user approval; frozen/reference read-only. Current-session command-output inspection is allowed, but never publish credentials/URLs/private YAML/raw log/env values; user may censor locally. This docs task has no DB contact/write, deletion or commit. P2-07 **OPEN/PARTIAL**; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-11 P2-07 gate: OLD_BOUNDED_INVENTORY_ONLY; OPEN/PARTIAL

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3`, status 95 with user work; source **37 / 21 managed**, unchanged .7 mirror SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`, .8 mirror `2F121E971506A71BB633084982510A88CE70B6FD41D154100E051BDA8E437D61`. Runtime NOT NULL science CV and .8 all-edge locked preflight are source-only. Reviewer 126 focused offline tests passed, F821/LSP clean; scoped Ruff **not clean** (existing F401/I001/SIM118/SIM210); full pytest/PG parser not run. Distinct READ ONLY candidate SHA-256 `CD0EFFABF971AB8703A4F7E2449CF61EAF3F9D7ABF147B8D04882721C30D6676` returned exact shell stdout `OLD_LEDGER_CATALOG_INVENTORY_BOUNDED` + `INVENTORY_EXIT=0`, exit 0; post HEAD/status95/hash `POST_SOURCE_PINS_MATCHED`, exit 0. Guarded PG18 TEST `grandgousier_070_gpx_full` OID `23069087` snapshot reported ledger 35 (32 BASELINED; .4/.5/.6 APPLIED; .7/.8 absent), three enforced old edge FK shapes, seven new named constraints absent. Reviewer reports-only result `OLD_BOUNDED_INVENTORY_ONLY`, not independent DB requery; no CV/member/edge rows, RI-trigger membership, sizes/caps, SCIENCE/frozen parity. Older 18/2,640/4/36/144 and fingerprint not revalidated; physical cluster identity/socket closure not established. Broad RO verifier preparation STOPped before artifacts; next separate CV/member RO check is **PLAN-only**, not authorized by this documentation update. Prioritize independent audit/approval and bounded resource gate before any such invocation; decide backup/PITR and writer fencing before a separately authorized DB write. `apply_pending` has separate .7/.8 commits; actual writer whole-transaction timeout and SET NULL triple-FK deletion behavior unknown. No DB connection/query or script/SQL execution in this docs update. `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### 2026-10-11 gate: A2/B .8 SOURCE-ONLY, NOT APPLIED (P2-07 OPEN/PARTIAL)

Source manifest **37 / 21 managed** includes additive .8 reference-membership deferred FK, immediate CV field/series composite FKs and two parent uniques; five .7 edge FKs and the original RESTRICT/CASCADE/SET NULL policies remain. The writer inserts generation before members; nullable membership series bypasses only the triple FK. No role/lifecycle freeze, science-formula/selection change, or whole-field deletion workflow. Last independent TEST snapshot is still old ledger 35 (.4/.5/.6 APPLIED; .7/.8 absent), three edge FKs, not a fresh read. Source/runtime requires both .7/.8. Ordinary migration applies each in separate commits; failure in .8 can leave .7 committed. Next: independent source/hash/Reviewer audit, separately requested exact one-shot approval and resource/backup decision before any DB action, then bounded acceptance/science/idempotency/whole-field deletion gates. No DB connection/query or SQL execution in this source task, no verified backup/PITR or physical-byte equivalence. Five ordered table ACCESS EXCLUSIVE locks and ledger SHARE, index builds, 5s lock and 30s per-statement timeouts need review; no whole-transaction guarantee. `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### Current gate — 2026-10-10 (A1_V2_RI30_CATALOG_ROLLBACK_ONLY_PASS_BOUNDED_NOT_APPLIED; P2-07 OPEN/PARTIAL)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / `0.7.0.10`, grouped status **93** incl. user work and digest `997B1784047F84EB242599ACD8852F14680B2218EDBFB8C4497690FD82158006` matched pre/post pins. The user approved exactly ONE NEW V2 rollback-only full captured source .7 SQL RI catalog trial with TWO ADD CONSTRAINT actions on non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`), accepting no verified backup/PITR for this one. External writers/schema workflows were user-attested paused through separate Reviewer Stage2, **not server-proven**; pause requirement completed at report delivery to Manager and may be released. Approval **CONSUMED**; frozen reference untouched.

**Stage1 provisional:** previously audited PowerShell 5.1 common+single-call gate assembly invoked pinned outside-temp V2 RI candidate SHA-256 `4C09DCF1CDABB7BB90B534DA96D647738A0273966FF5AC53A8F0A2F4FCD6FE62` **exactly once**. Sanitized wrapper result exactly `PROVISIONAL_STAGE1_RI30_ROLLBACK_ONLY_STAGE2_PENDING`, wrapper exit **0**, requiring one fixed candidate PASS/native exit **0 inferred from wrapper, not separately printed**, no extra stderr, matching pre/post path/hash/Git pins. Candidate reported full captured .7 SQL executed once (two FK additions) and transient **11 outgoing M4 FKs, five edge FKs, four inbound FKs and RI30 shapes**, using five BIGINT casts and forced new-FK checks; explicit outer ROLLBACK and internal separate old-state RO check. No ledger INSERT/commit/DELETE/SCIENCE-row scan. Transient RI30 is **candidate-reported; not independently requeried while DDL existed**.

**Separate Reviewer Stage2 independent old state:** pinned outside-temp oldstate-v2 direct-psycopg REPEATABLE READ READ ONLY verifier SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3` ran **once in a separate PowerShell process** via audited pins. Exact wrapper stdout `PASS_INDEPENDENT_STAGE2_OLDSTATE_BOUNDED`, fixed verifier PASS/native exit **0 inferred from wrapper**, pre/post HEAD/grouped-93 status digest matched. TEST snapshot: ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), old **three** edge FKs/new pair absent across `public`, **22** old RI trigger checks, exact **18/2,640/4/36/144** series/series-frame/generations/members/all-status edges, zero scoped anomalies, relation caps and fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`. The 2,640 `field_series_frames` were checked, **not writer-fenced**. Reviewer recommends **`BOUNDED_RI30_ROLLBACK_TRIAL_PASS_NOT_APPLIED`** on Stage1 candidate-reported transient state plus independently observed Stage2 old state, **not** an independent transient RI30 check or permanent application.

**Next gates OPEN:** Source .7 manifest **36 / 20 managed**, packaged/scripts raw SQL SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`, source unchanged; permanent .7 TEST application pending (Stage2 old ledger35/edge3), managed runner commit not exercised. Earlier 2026-10-10 V1 RI Stage1 STOP/V1 RO STOP and V2 old-catalog JSON RO PASS remain dated; 2026-10-09 V3 rollback-only trial is separate. Creator-first timeout/event smoke **UNKNOWN**. No verified backup/PITR, physical-byte equality, proven socket closure or full SCIENCE/old-new/frozen parity; WAL/locks/physical growth possible. A2/B, manual coordinated member-plus-edge DELETE, ordered whole-field deletion, full science/pytest/release OPEN. No DB connection/query, SQL/DDL/DML execution or script invocation in **this docs-only task**; no further DB write approval. P2-07 OPEN/PARTIAL; `READY_FOR_DB_WRITE=NO`, `READY_FOR_EXECUTE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### Previous gate — 2026-10-10 (OLD_CATALOG_JSON_V2_READONLY_PASS_BOUNDED_ONLY; P2-07 OPEN/PARTIAL)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / version `0.7.0.10`, grouped status **93** incl. user work, SHA-256 `997B1784047F84EB242599ACD8852F14680B2218EDBFB8C4497690FD82158006`, and pre/post pinned source/test/SQL/Git/Python hashes matched. Exactly **one** newly approved, previously independently bytes-audited V2 OLD catalog JSON script SHA-256 `C8D8BA23B1353788974F0CAF908C4C69ED38CB672F94A53697EE5DFF5E630C22` ran READ ONLY via the independently audited PowerShell 5.1 wrapper on non-frozen PG18 TEST `grandgousier_070_gpx_full` (OID `23069087`). Fixed sanitized stdout: `PASS P207_A1_OLD_CATALOG_JSON_V2_READONLY old_fks=9 inbound=2 ri_triggers=22 catalog_json=PARSED ri30=NOT_PROVEN stage1_cause=UNKNOWN`. Wrapper PASS required exact child stdout and native exit **0 inferred, not separately printed**. Reviewer `REPORT_AUDIT=PASS_BOUNDED_OLD_JSON_RO` was **reports-only, no DB rerun**. Frozen reference not contacted; latest one-shot RO approval consumed.

One guarded direct-psycopg REPEATABLE READ READ ONLY snapshot checked target/user/PG18/OID/timeouts, old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), **nine outgoing old M4 FKs / two inbound FKs / 22 RI triggers**. One corrected V2 `CATALOG` SELECT with five OID::bigint casts returned text JSON parsed with strict Python integer checks. No DDL/DML, SCIENCE/frame-row scan or advisory lock. Source .7 manifest **36 / 20 managed**, packaged/scripts SQL SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7` unchanged, .7 **not permanently applied**. V2 RI candidate SHA-256 `4C09DCF1CDABB7BB90B534DA96D647738A0273966FF5AC53A8F0A2F4FCD6FE62` remains **OFFLINE ONLY** (four V2 temp fake suites, **88 passed combined offline**), no live RI DDL for it.

**Next gates remain OPEN:** RI30/new database FK pair, root cause/stage of prior STOPped V1 RI writer or V1 old-catalog RO probe, full dataset/SCIENCE/physical-byte parity, physical socket closure and managed writer timeout are **NOT PROVEN** by this probe. Earlier dated 2026-10-10 Stage1 STOP and independent Stage2 old-state PASS below are not overwritten; 2026-10-09 V3 success is a separate rollback-only trial. Creator-first RO timeout smoke UNKNOWN; no verified backup/PITR; prior RI DDL waiver consumed, **no active pause**. A2/B, coordinated manual member-plus-edge DELETE, ordered whole-field deletion, full science/old-new/frozen acceptance and release remain OPEN. No DB connection/query or SQL/script invocation in *this docs-only update*; full pytest not run. P2-07 OPEN/PARTIAL; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### Previous gate — 2026-10-10 (RI_TRIAL_STOP_OLD_STATE_RO_CONFIRMED_NOT_RI30; P2-07 OPEN/PARTIAL)

At HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / version `0.7.0.10`, grouped status **93** incl. user work remained pinned before/after both stages. Source .7 manifest **36 / 20 managed**, packaged/scripts SQL mirror SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`, **not permanently applied** on non-frozen TEST `grandgousier_070_gpx_full` (PG18, OID `23069087`). The user approved exactly **one** new rollback-only whole-.7-SQL RI catalog trial with **two ADD CONSTRAINT**, accepted **no verified backup/PITR** for this trial and attested external writer/schema pause through separate Reviewer RO postcheck (not server-proven). Approval **CONSUMED**, no retry; pause requirement completed at Stage2 report delivery and may be released. Frozen reference never contacted.

Stage1: **one** audited pinned RI candidate SHA-256 `ED970F7195722BEC10DD04B51ABED9DF8667A5A559E86FE03B27D51C6D4A9DF9` returned wrapper **`STOP_RI_TRIAL_NOT_PASS_NO_RETRY`**, fixed candidate STOP (raw stderr not printed), native exit **1 inferred from wrapper**, pre/post HEAD/grouped-93 digest pins matched. DB connection, transient DDL and explicit rollback are **unknown**; **RI30 transient catalog shape NOT ESTABLISHED**, trial not PASS. Stage2: **one** separate pinned old-state-v2 direct-psycopg READ ONLY verifier SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3` via audited Stage2 returned **`PASS_INDEPENDENT_OLDSTATE_RO_BOUNDED`**, fixed verifier PASS/native exit **0 inferred**, pre/post pins matched. At that snapshot: old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), old **three** edge FKs and both new names absent across `public`, old RI **22** and inbound FKs, exact **18/2,640/4/36/144**, zero scoped anomalies, relation caps and fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`. The **2,640** series-frame rows were checked, **not writer-fenced**. Bounded old logical state at one snapshot cannot turn Stage1 STOP into RI30 PASS or prove transient behavior/physical-byte equality.

**Next gates remain OPEN:** the different successful V3 SQL-plus-provisional-ledger rollback-only trial below retains its **2026-10-09 dated scope**, not RI30 approval. Recent creator-first RO timeout/event smoke proposed invocation output is **UNKNOWN after agent usage limit**: no PASS or retry. No verified backup/PITR, full SCIENCE/old-new/frozen parity or proven socket closure; WAL/locks/physical growth possible. No permanent .7 managed apply, A2/B or coordinated manual member-plus-edge DELETE, ordered whole-field deletion, full pytest or release acceptance. No DB connection/query, SQL, DDL/DML or retry for *this docs-only correction*. P2-07 **OPEN/PARTIAL**; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### Historical gate — 2026-10-09 (P2-07 A1 .7 trial passed BOUNDED, rolled back; OPEN/PARTIAL)

Unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3`, source `0.7.0.10`, 93 pre-existing Git status entries preserved. Source .7 remains **36 / 20 managed**, packaged/scripts raw SHA-256 `8C987A810D1B93A43DBE9E3B8E295915AF1DBD82E9B6DC3210330BA7B7BF13A7`. The user approved exactly **one** rollback-only A1 .7 trial on non-frozen TEST `grandgousier_070_gpx_full` (PG18, OID `23069087`), accepting no verified backup/PITR and attesting an external writer/schema pause through Reviewer postcheck. Approval **CONSUMED**; pause requirement completed at Stage2 report delivery and can be released (not server-proven). Frozen reference never contacted.

Stage1 ran once: pinned V3 candidate SHA-256 `22905165E6196A0217BA5FEB888911BB4043DA257D043F6D7F44D04C0475F609`; wrapper classified `PROVISIONAL STAGE1_ROLLBACK_ONLY_NOT_APPLIED INDEPENDENT_RO_PENDING`. Candidate PASS ledger **35**, old **three** edge FKs, exact **18/2,640/4/36/144** series/series-frame/generation/member/all-status-edge counts, bounded witness `UNCHANGED`, `physical_bytes=NOT_ASSERTED`, `SCRIPT_EXIT_CODE=0`, matched pre/post HEAD/status93 pins (digest prefix `997B...`). It reported **two transient composite FK DDL statements and a provisional .7 ledger INSERT** in one writer transaction, outer **ROLLBACK** and a separate internal RO postcheck. No durable .7 commit, `apply_pending()`, DELETE or SCIENCE-row scan.

Stage2 independent Reviewer direct-psycopg RO verifier SHA-256 `7A0EE9B7C6C4C75A04FD7AE5199BA7BF354BB9010F708E2740233D8D707FBDE3` ran **once**, `PASS STAGE2_INDEPENDENT_OLDSTATE_READONLY`, exit **0**, with matching fixed RO PASS and pre/post pins/status baseline. It verified TEST old ledger **35** (32 BASELINED; .4/.5/.6 APPLIED; .7 absent), old three edge FKs and both new names absent across `public`, exact **18/2,640/4/36/144**, **22 RI trigger checks/inbound FKs**, relation caps, zero scoped anomalies and fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`. Reviewer recommends **`TRIAL_PASS_ROLLBACK_ONLY_NOT_APPLIED` BOUNDED**, not DB migration applied. The 2,640 `field_series_frames` were checked but not writer-fenced.

**Next gates:** no verified backup/PITR, physical-byte equality, full science/frozen parity or proven physical socket closure; rollback may cause WAL/locks/physical growth. Manual coordinated member-plus-edge deletion untested; A2 reference-membership, B CV pair/triple, separately approved ordered whole-field deletion and full science/old-new/frozen acceptance OPEN. Full pytest/release NOT RUN. This documentation-only update makes no DB connection or execution. P2-07 **OPEN/PARTIAL**; `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO` (not requested).

### Historical gate — 2026-10-09 (P2-07 A1 source-only, not applied; OPEN/PARTIAL)

HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3`, source `0.7.0.10`;
93 pre-continuation dirty status entries preserved, no commit. Worktree now contains
managed .7 A1 source SQL (packaged/scripts byte-identical SHA-256
`8c987a810d1b93a43dbe9e3b8e295915af1dbd82e9b6dc3210330ba7b7bf13a7`),
manifest **36 / 20 managed**, ORM and runtime contract for five edge FKs, and
focused offline tests (**120 passed**). The two composite edge→same-generation-member
FKs use `NO ACTION DEFERRABLE INITIALLY DEFERRED`; .6 direct series endpoint
`RESTRICT` and generation `CASCADE` remain. PG15/17/18 catalog-enforcement paths
are offline-tested, **not** live verified. Resource/checksum/mirror, repository F821,
isolated compileall, touched Python LSP and diff --check passed (line-ending warnings);
scoped Ruff has only the known pre-existing F401 in `tests/test_migration_ledger.py:14`.

Five SQL locks (four ordered `ACCESS EXCLUSIVE` plus a fifth ledger `SHARE`) precede
the locked READ COMMITTED 35-predecessor ledger, catalog and all-edge preflight.
The 5s lock/30s per-statement timeouts do **not** bound the whole transaction;
ledger lock contention/deadlock remains possible. No SQL harness, PostgreSQL parser
or live .7 test, DB connection, DDL/DML, or migration execution occurred in this
source continuation. Last *documented* TEST `grandgousier_070_gpx_full` (OID
`23069087`, PG18) has .6 APPLIED, **35 ledger** and **three old edge FKs** after
the rollback-only trial, not .7 applied; frozen reference untouched. No verified
backup/PITR. Manual member-plus-edge deletion remains untested (only trial
generation-CASCADE positive case passed); A2 reference-membership and B CV
pair/triple, ordered whole-field deletion, full science/frozen parity, full pytest
and release validation remain OPEN. No write approval: `READY_FOR_DB_WRITE=NO`,
`READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`,
`READY_FOR_EXECUTE=NO`, `READY_FOR_APPLICATION=NO`, `READY_FOR_COMMIT=NO`.

### Historical gate — 2026-10-07 (P2-07 A1 rollback-only trial PASS, not applied; still true)

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` with all 91 pre-existing
dirty entries preserved and no commit, non-frozen TEST `grandgousier_070_gpx_full`
(OID `23069087`, PG18) still has **.6 APPLIED_TEST_VERIFIED**, ledger **35** and the
old **three** edge FKs; frozen reference untouched. User approval for exactly **one**
rollback-only A1 DDL/DML trial, without verified backup/PITR and with an independently
attested pause of external writers/schema changes, is **CONSUMED**. The pinned pretrial
READ ONLY witness `grandgousier_p207_a1_witness_readonly_20261007.py` (SHA-256
`55CFD5D3FFA1AAC998BE814D51EDA173356B59A5E96DD0CE04B82EBC0352349C`)
passed inbound FKs/22 RI trigger functions, 35/18/4/36/144 ledger/series/generation/
member/edge checks and safe left/right/non-reference-member/generation witnesses;
fingerprint `551682CBCF7D0F3109A58823314AC1C692AF8AC4835908F29B34E8A35AE821F6`.
Its **86 fake tests passed OFFLINE**; the read-only snapshot was not an enduring lock.

Trial-only SQL `grandgousier_p207_a1_trial_fks_20261007.sql` (SHA-256
`0D783A599B125E4B7BEF81BAA0C23AD73B11C85054A04E704C8A2593F0C9D92D`)
added two composite edge `(generation,left/right)` → membership `(generation,series)`
`ON DELETE/UPDATE NO ACTION DEFERRABLE INITIALLY DEFERRED` FKs inside **one outer
transaction only**. Harness `grandgousier_p207_a1_rollback_trial_20261007.py`
(SHA-256 `7E09BCC4136C8BD84A371E1493BBD538CE59D0FDB3D60DB2901046A8D129A632`)
passed **32 fake tests OFFLINE**; Reviewer pre-audit `READY_FOR_USER_REVIEW=YES` was
not permission to write. The one approved invocation exited **0**:
`PASS P207_A1_ROLLBACK_TRIAL_POSTCHECK ledger=35 counts=18,2640,4,36,144 logical_witness=UNCHANGED physical_bytes=NOT_ASSERTED`.
After four ordered `ACCESS EXCLUSIVE` locks and an under-lock recheck of ledger 35,
old three edge FKs, inbound FKs/22 RI trigger functions, caps and fingerprint, the
in-transaction catalog showed five edge FKs with existing .6 endpoint RESTRICT and
generation CASCADE intact. Separate savepoints rejected invalid existing-edge left/
right updates and direct non-reference member DELETE with exact new FK SQLSTATE
**23503** after `SET CONSTRAINTS IMMEDIATE`; generation DELETE cascaded members/edges
and passed both forced FK checks. Savepoints restored; outer ROLLBACK, no commit,
ledger insert, managed migration, field-series or whole-field deletion. Separate
guarded READ ONLY internal postcheck passed old three FKs, ledger 35, exact
18/2,640/4/36/144, unchanged bounded logical witness and size caps, **not byte parity**.
Reviewer inspected source/output but did not rerun the live trial. Manager invoked
the same witness READ ONLY **once** after the trial: PASS same fingerprint, ledger
35, 18/4/36/144, inbound FKs/triggers, `A1_POSTCHECK_EXIT=0`; old three-edge-FK
state confirmed, not independent transient SQLSTATE confirmation.

**Next gates:** no persistent A1 FK or A1 source migration; coordinated manual
member-plus-edge deletion **UNTESTED** (only generation CASCADE positive case);
future writes/whole-field behavior not exhaustive. A2 reference-membership and
B CV pair/triple **NOT IMPLEMENTED**. Rollback may produce WAL, locks or physical
growth; no verified backup/PITR, proven socket closure or full physical-byte/science
parity. Full science, full pytest and release validation **NO**. No further DB write
approved: `READY_FOR_EXECUTE=NO`, `READY_FOR_DB_WRITE=NO`,
`READY_FOR_SCHEMA_CHANGE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`. This documentation
change made no DB connection. Prior .6 application remains true for TEST; the
dated gates below are historical, not authority to apply A1.

### Historical gate — 2026-10-07 (prior .6 APPLIED_TEST_VERIFIED on TEST; still true)

At unchanged HEAD `8a370d65cad383337d2a55d8ec72aecf0ddb18e3` / source `0.7.0.10`,
the user selected option `2`, accepting no verified backup/PITR for exactly one permanent
0.7.0.6 SQL-plus-ledger commit on **non-frozen TEST only** `grandgousier_070_gpx_full`
(OID `23069087`, PostgreSQL 18). External writer/schema pause was user-attested through
postcheck, not server-proven. One approved managed-script invocation exited 0 with
`PASS CANDIDATE_POSTCHECK ledger=35 series=18 frames=2640 generations=4 memberships=36 edges=144 physical_bytes=NOT_ASSERTED`;
one `apply_pending()` call committed the .6 SQL and ledger together (runner control
transactions also commit, advisory lock released on normal return). A separate one-shot
independent direct-psycopg REPEATABLE READ READ ONLY Reviewer postcheck exited 0 with
`PASS INDEPENDENT_NEW_STATE_POSTCHECK ledger=35 .6=APPLIED new_edges=2 generation_cascade=YES counts=18,2640,4,36,144 anomalies=0 size_caps=PASS`.
The Reviewer verified 35 matching ledger entries (32 BASELINED, .4/.5/.6 APPLIED once),
zero pending/drift, two nondeferrable edge endpoint RESTRICT FKs, preserved
generation/membership/reference/SCIENCE-CV actions, exact scoped counts and zero anomalies;
relevant relation sizes ≤64 MiB. No SCIENCE photometry rows were scanned in the new-state
postcheck; no DELETE probe, rebuild or SCIENCE scan in the managed application. Candidate
managed-wrapper fake suite **188 passed**, independent Reviewer fake suite **114 passed**
(separate runs, not full pytest). Frozen reference untouched; worktree/user changes
preserved, no Git commit.

The prior rollback trial's strict physical-byte mismatch for `calibration_versions`
and `artifacts` remains **unresolved**, not logical row-loss evidence; pre-managed-apply
read-only totals of **1,048,576** and **45,645,824** bytes are **not** claimed as new-state
sizes. New bounded size caps do not prove historical byte equality, socket closure or
row-by-row/science parity. No verified full dump/restore; locks/WAL may occur. Next gates:
composite edge→same-generation membership and CV future-write constraints, separately
approved ordered whole-field deletion, full science/old-new/frozen parity, full pytest
and release validation. **APPLIED_TEST_VERIFIED is not archive/frozen-production READY.**
No further DB write or migration is approved: `READY_FOR_EXECUTE=NO`,
`READY_FOR_DB_WRITE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`.

### Historical gate — 2026-10-07 (rollback-only trial; physical postcheck STOP; previous delta)

The following is the dated **pre-managed-application** rollback gate, not the current ledger state.

Source migration `mvp7_0_6_interseries_edge_delete_restrict` is present but **not permanently
applied** (source manifest 35 total / 19 managed). One explicitly approved rollback-only
TEST trial verified both named endpoint RESTRICT FKs and two selected single-series DELETE
savepoint refusals (SQLSTATE **23001**); no ledger commit or whole-field DELETE. The
old-state schema/ledger and checked logical counts matched after rollback, but the
strict physical byte-size postcheck **failed** for `calibration_versions` and `artifacts`;
trial exited **1** (`STOP OLD_STATE_MISMATCH`). Independent guarded READ ONLY old-state
preflight passed; current read-only size checks were below 64 MiB but do not establish
the missing historical byte delta or cause. Do not promote transient endpoint behavior
to permanent managed migration or science acceptance. The one-shot writer approval is
consumed: any further consequential action requires new explicit approval. Next gates
remain composite edge→generation-membership and CV future-write scope, approved ordered
whole-field deletion, permanent migration/ledger and full science/old-new/frozen parity,
full pytest and release validation. Frozen reference untouched; `READY_FOR_EXECUTE=NO`,
`READY_FOR_DB_WRITE=NO`, `READY_FOR_MIGRATION_APPLICATION=NO`.

### Previous gate — 2026-09-28 (bounded live validation)

At the 2026-09-28 gate (package/source 0.7.0.10): **BOUNDED_LIVE_VALIDATION** on
`grandgousier_070_gpx_full` (OID `23069087`). All 18 current Stage-2 heads are
`READY_STAGE3_RECIPE`; M4 generations V3/R4 are independently verified. M5 produced
134 unique candidate IDs (234 per-series occurrences; candidates are not confirmed
variables). A two-series Parquet smoke passed for series 1/V and 3/R, 50,000 rows each.
The frozen `grandgousier` reference DB remains untouched/read-only.

At that gate, outstanding work included the separate 2018-09-16 Stage-1 filesystem
reprocess (544 raws downloaded, 186/544 new reduced products, 358 remaining; no DB/alias
changes so far),
manual light-curve/candidate validation and full old/new/reference acceptance following
the bounded scientific M4/M5 audit, full 18-series Parquet export and archive-scale
performance evaluation, and release acceptance. Many Stage-2 frames and series are
degraded; the requested/computed color transform was not activated because its
pivot-color-fraction gate failed (~0.89 > 0.25). The two-series Parquet smoke does not
establish full-export readiness. Dated milestones below are retained as historical
plans, not current instructions.

### Historical checkpoint (2026-09-21; superseded)

The following checkpoint records the then-current 0.7.0.9 source candidate and pre-Stage-3
parity blocker; that blocker was fixed, and the bounded Stage-3 validation is now recorded
above.

## 0. Неподвижные условия

Не делать destructive in-place migration production DB.

Не удалять старую `grandgousier` до завершения old/new acceptance и отдельного согласия пользователя.

Не менять frozen Stage-2/Stage-3 production-v1 formulas/selection semantics в storage/schema patch.

MASTER `UNIQNUM` и RoboPhot `SERNUM+DATE-OBS(UTC)` остаются authoritative observation identity.

Нормальный pipeline не должен включать `--materialize-intermediate-calibrations` при clean rebuild.

## 1. Phase 0 — source baseline и pre-change audit

Перед первой правкой следующего чата:

1. подтвердить актуальный source tree `0.6.4.5.1`;
2. прочитать `CURRENT_ARCHITECTURE.md` и `ROADMAP.md`, затем открыть релевантные исходники
   и tests;
3. прочитать actual models/schema/bootstrap и relevant readers/writers;
4. проверить tests, которые зафиксируют 0.6.4.5.1 behavior;
5. не выполнять никаких DB commands;
6. сформировать dependency matrix каждой таблицы/колонки, затрагиваемой 0.7.0-A;
7. решить, является ли 0.7 отдельной fresh-baseline schema без backward DB compatibility (предпочтительно) или нужен compatibility adapter; production DB не мигрируется.

Exit gate: точный patch scope без guessed schema и без destructive operations.

## 2. Phase A — 0.7.0-A clean schema foundation — WINDOWS REAL-VALIDATED

Это **первый обязательный implementation patch**.

### A1. BASE-only measurements

Цель:

```text
measurements = immutable BASE extraction truth only
```

Сделать:

- `calibration_version_id NOT NULL`;
- enforce/validate `product_kind=BASE_PHOTOMETRY`, `product_role=BASE`, `COMPLETE_IMMUTABLE` для calibration versions, которым принадлежат BASE rows;
- убрать/запретить code paths, которые в normal mode пишут derived full rows;
- compatibility option не должен определять clean schema;
- добавить tests, что default pipeline не создаёт non-BASE measurements.

Не делать пока BASE flags factorization: сначала clean smoke physical profile.

### A2. Durable final science

Спроектировать `science_photometry` вместо rebuildable-name `science_photometry_cache`.

До реализации определить exact durable scalar contract по reconstruction/API consumers. Не копировать wide V1 автоматически.

Минимум должен поддерживать:

- Stage 3 planning/analysis/persistence;
- M5/global object;
- current light curves;
- stable SQL query surface;
- object/frame identity через BASE reference.

Нужно решить migration/API strategy: rename vs new table в fresh baseline. Поскольку production DB не мигрируется, допускается clean break 0.7 при сохранении Python compatibility boundary.

### A3. Убрать fresh-schema historical compatibility baggage

Candidates to omit from 0.7 fresh schema after reader audit:

```text
science_measurements
science_flag_override_sets
science_flag_overrides
interseries_calibration_offsets
```

Если current source импортирует models/readers этих tables, сначала сделать code cutover, затем удалить из schema contract/models. Не удалять вслепую.

### A4. Series membership

Убрать `UNIQUE(frame_id)` из `field_series_frames`.

Сохранить `(series_id, frame_id)` uniqueness.

Добавить explicit reproducible grouping definition/version там, где лучше всего соответствует существующей `field_series` model.

Тест: один Frame может легально принадлежать двум alternative series definitions, при этом duplicate membership в одной series запрещён.

### A5. Normalized calibration integrity

Предпочтительно удалить redundant `calibration_version_id` из:

```text
calibration_frame_terms
calibration_iteration_objects
```

и получать через `calibration_iterations`.

Перед этим проверить все direct SQL/ORM readers и indexes. Если removal слишком широк для A, временный допустимый вариант — composite integrity FK/unique pair, но финальный target остаётся без duplicated identity.

### A6. Provenance FK policy

Проверить `SET NULL` на scientific lineage (`CalibrationVersion`, artifacts, QC etc.). Для clean immutable provenance использовать `RESTRICT/NO ACTION`, где удаление parent означало бы потерю scientific lineage. Не менять operational relations механически.

### A7. Fresh bootstrap contract

`init-db` должен создать clean 0.7 schema из ORM и baseline migration ledger.

Добавить isolated test на абсолютно пустую PostgreSQL schema/database-like environment, насколько позволяет test infrastructure. Не строить fresh bootstrap через legacy migration replay.

### A8. A-phase tests

Минимум:

- model/schema contract tests;
- `init-db` fresh baseline tests;
- default pipeline storage mode tests;
- reconstruction/calibration view tests;
- Stage3 cache/science reader tests;
- series membership tests;
- acceptance-service unit/integration tests без production DB;
- broad pytest + compileall;
- release_check по существующей политике.

Implementation и Windows/Python 3.11 release gate закрыты: `704 passed, 1 skipped`; compileall и release packaging/install smoke OK. Production DB untouched.

## 3. Phase B — detection storage normalization — WINDOWS REAL-VALIDATED

Source version: **0.7.0.1**. Production DB untouched.

### B1. `detection_runs` — IMPLEMENTED

Добавлены `detection_runs` + `detection_run_frames`. Run-level algorithm/version/config/fingerprint/pipeline provenance хранится один раз; frame-level status/diagnostics — один раз на frame/run. Zero-detection/zero-match frames сохраняются явно.

`source_detections` больше не содержит repeated `frame_id`, `algorithm`, `parameters` или frame-common detection diagnostics; остаются source-specific measured quantities/status/flags + run-frame reference.

### B2. Match/projection slimming — PARTIAL/CONSERVATIVE

Real readers/writers audited. `detection_catalog_matches` переведён на run/source/catalog graph и лишён reconstructable repeated frame/catalog/algorithm/details payload. `detection_mag_inst`, `catalog_mag`, `zero_point` сохранены как immutable historical snapshots, потому что current catalog/reference state может обновляться.

`frame_catalog_links` **не менялся**: его slimming/index changes запрещены до реального workload EXPLAIN.

### B3. Physical/query benchmark — DEFERRED GATE

После A–D на отдельной fresh GPX smoke DB сравнить rows/bytes/indexes/query plans до/после. Не экстраполировать synthetic/capped sample как global saving. Если join plans регрессируют, допускается вернуть только доказанную typed denormalization.

Source implementation gate закрыт. Windows/CPython 3.11: `709 passed, 1 skipped`, `compileall` OK, release check `READY_WITH_WARNINGS` только из-за не переданных validated constraints/snapshot; wheel/sdist/resources/install-smoke/checksums OK. Physical EXPLAIN/bytes exit gate остаётся post-A–D fresh-smoke условием до archive-wide rebuild.

## 4. Phase C — identities and locations — WINDOWS REAL-VALIDATED 0.7.0.2

### C1. Object identity — IMPLEMENTED

`objects.object_uid` теперь opaque internal UUID; catalog/source ID и координаты не входят в UID. `object_catalog_links` хранит explicit catalog identity/evidence.

Association contract: existing catalog link -> ровно один astrometric candidate в 0.75 arcsec с epoch/proper-motion propagation -> новый Object. Candidate, уже связанный с другим source ID того же `catalog_name`, исключается из astrometric merge; разные источники одного catalog release не схлопываются. Если candidates несколько, auto-merge запрещён: создаётся новый Object. Это conservative policy против false merges; отдельный evidence-driven merge можно добавить позже.

### C2. Frame locations — IMPLEMENTED

`frame_locations` хранит mutable locator, size/mtime, availability и optional SHA-256. Fresh `frames` не содержит locator/file-metadata columns; Python compatibility properties читают primary location.

`raw_path` больше не identity. MASTER/RoboPhot observation identity не менялась; content hash — только дополнительное content/location evidence.

### C3. Replay/moved-file tests — WINDOWS REAL-VALIDATED

Preflight/indexer различают новый product и relocated COPY_LIKE product по observation identity + non-locator product metadata. Одно physical product может иметь несколько locations без создания нового Frame/Observation.

Windows/CPython 3.11: targeted regressions `3 passed`, полный pytest `714 passed, 1 skipped`, `compileall` OK; release check `READY_WITH_WARNINGS` только из-за не переданных validated constraints/snapshot.

## 5. Phase D — normalized reference photometry — IMPLEMENTED SOURCE CANDIDATE 0.7.0.3

Fresh layout:

```text
reference_photometry_catalogs
        ↓
photometric_bands + reference_photometry_sources
        ↓
reference_photometry_points(source, band, magnitude, error, quality/provenance)
```

Active runtime больше не читает wide `reference_photometry`. Import/query/color-calibration/auto-process paths переведены на normalized representation; новые полосы импортируются как rows, а не schema columns. Compatibility payload сохраняет текущие `mag_g/.../mag_g_gaia`, colors и error keys для существующей calibration logic. Wide pre-D table остаётся только в frozen `LegacyBase`, marker D fail-closed запрещает in-place conversion.

Source gate: `619 passed, 42 skipped` broad available suite, targeted D/schema/color `80 passed, 1 skipped`, `compileall` OK. Exit gate: Windows full pytest + release packaging/install smoke. После него переходить к Phase E fresh DB bootstrap.

## 6. Phase E — fresh DB bootstrap

Только после A–D.

Не использовать production app credentials для administrative connection без проверки.

Сначала определить текущий connection source (env/config) без вывода password. Если user `grandgousier` не имеет CREATE DATABASE, использовать explicit admin procedure вне приложения; не менять `pg_hba.conf` автоматически.

После создания fresh DB:

```text
init-db
schema-status
doctor --strict-optional
```

Снять fresh schema snapshot и сравнить с 0.7 contract.

Ожидание: science/data rows = 0, migration ledger clean-baselined, no compatibility baggage unless explicitly retained.

## 7. Phase F — GPX_TF17_2 clean smoke rebuild

Обработать только исходные/raw inputs GPX_TF17_2. Не reuse old DB-derived caches/materializations, если они могут скрыть bootstrap/storage defects.

Сравнить old/new по:

- physical observation identities;
- frame/series logical membership;
- Stage2 READY coverage;
- exact science values/tolerances по frozen contract;
- Stage3 graph topology/offsets/statistics;
- M5 source/accepted/rejected + reasons;
- output fingerprints где meaningful;
- storage composition and bytes.

Не требовать одинаковых surrogate IDs.

## 8. Phase G — idempotency gate

Повторить тот же dataset тем же current 0.7 workflow.

Доказать:

```text
new observations = 0
unexpected new frames = 0
unexpected duplicate BASE rows = 0
unexpected new calibration generations = 0 при identical input fingerprint
unexpected new science rows = 0
non-BASE measurements = 0
```

Проверить series membership и artifacts/pipeline run semantics.

## 9. Phase H — archive-wide rebuild

Только после F/G.

Запускать по инструментам/датам/полям контролируемыми batches с периодическими gates:

- DB size/row growth;
- duplicate observation identities;
- BASE materialization factor;
- science rows relative to BASE;
- detection bytes/row;
- failed/partial jobs;
- ANALYZE/autovacuum health;
- selected science acceptance controls.

Не ждать конца всего архива, чтобы обнаружить storage regression.

## 10. Phase I — old DB retirement

Только после полного rebuild и explicit user decision:

1. final acceptance old/new;
2. export schema snapshot + counts + checksums + critical manifests;
3. DB dump/archive strategy;
4. verify dump restore if archive required;
5. затем отдельно решить DROP old DB.

Никакого автоматического DROP в application patch.

## 11. Validation discipline / pitfalls

- неизвестные columns/table semantics сначала inspect, потом SQL;
- absence of FK != reconstructability;
- exact historical flags != scalar parity;
- PowerShell environment не имеет `rg` по умолчанию;
- nullable psycopg binds нельзя использовать в untyped `IS NULL OR` selector;
- SQLAlchemy bind casts через `CAST()`;
- qualify joined columns;
- exact counts отдельно от stale planner estimates;
- production DB read-only reference на весь 0.7 migration project.

## 12. Ближайший следующий шаг

После Windows validation 0.7.0.3 следующая работа начинается **с Phase E fresh DB bootstrap**, а не с дальнейшего schema refactor. Phase B physical/query benchmark остаётся частью fresh GPX gate до archive-wide rebuild.

Expected deliverable первой итерации:

```text
Grandgousier_0.7.0-A_clean_schema_foundation_patch.zip
```

rootless overlay, без лишних README/release-note/handoff файлов, с обновлением canonical context docs только если patch реально меняет состояние/архитектуру.
