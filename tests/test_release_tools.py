from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_check = _load("gg_release_check", ROOT / "tools" / "release_check.py")
snapshot = _load("gg_snapshot_environment", ROOT / "tools" / "snapshot_environment.py")


def test_release_version_consistency_matches_project():
    pyproject_version, init_version = release_check.read_versions()
    assert pyproject_version == "0.7.0.11"
    assert init_version == pyproject_version


def test_constraints_lines_are_stable_and_exact():
    lines = snapshot.constraints_lines([
        {"name": "NumPy", "canonical_name": "numpy", "version": "2.1.3"},
        {"name": "Astropy", "canonical_name": "astropy", "version": "7.1.0"},
    ])
    assert lines[-2:] == ["Astropy==7.1.0", "NumPy==2.1.3"]


def test_check_constraints_requires_core_dependency_names(tmp_path):
    path = tmp_path / "constraints.txt"
    names = [
        "astropy", "numpy", "pandas", "sqlalchemy", "psycopg", "pydantic",
        "pydantic-settings", "PyYAML", "tqdm", "matplotlib", "certifi", "requests",
    ]
    path.write_text("\n".join(f"{name}==1.0" for name in names), encoding="utf-8")
    result = release_check.check_constraints(path)
    assert result.status == "OK"


def test_check_snapshot_binds_version_pyproject_and_complete_closure(monkeypatch, tmp_path):
    version, _ = release_check.read_versions()
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=(), version=version)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    path = tmp_path / "environment_snapshot.json"
    payload = {
        "format": "grandgousier_environment_snapshot_v2",
        "grandgousier_version": version,
        "pyproject_sha256": release_check.sha256_file(project),
        "missing_required_packages": [],
        "selected_extras": [],
        "package_count": 0,
        "packages": [],
        "dependency_closure": {
            "format": release_check.SNAPSHOT_CLOSURE_FORMAT,
            "complete": True,
            "states": [],
            "unresolved_requirements": [],
        },
        "python": {"version": "3.11.0"},
        "platform": {"system": "Windows"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = release_check.check_snapshot(path, version=version)
    assert result.status == "OK"
    assert "Windows" in result.detail

    payload["format"] = "grandgousier_environment_snapshot_v1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    legacy = release_check.check_snapshot(path, version=version)
    assert legacy.status == "FAIL"
    assert "mismatch=format" in legacy.detail


def test_wheel_inspection_rejects_private_paths_and_accepts_resources(tmp_path):
    wheel = tmp_path / "grandgousier-0.5.58.1-py3-none-any.whl"
    metadata = "Metadata-Version: 2.4\nName: grandgousier\nVersion: 0.5.58.1\nRequires-Python: >=3.11\n"
    with zipfile.ZipFile(wheel, "w") as zf:
        for resource in release_check.REQUIRED_WHEEL_RESOURCES:
            zf.writestr(resource, "x")
        zf.writestr("grandgousier-0.5.58.1.dist-info/METADATA", metadata)
        zf.writestr("internal_tools/private.py", "secret")
    checks = {row.name: row for row in release_check.inspect_wheel(wheel, "0.5.58.1")}
    assert checks["wheel_private_paths"].status == "FAIL"
    assert checks["wheel_resources"].status == "OK"
    assert checks["wheel_metadata"].status == "OK"


def test_sdist_inspection_requires_release_tools_and_rejects_internal(tmp_path):
    import tarfile
    import io

    sdist = tmp_path / "grandgousier-0.5.58.1.tar.gz"
    files = {
        "grandgousier-0.5.58.1/tools/release_check.py": b"x",
        "grandgousier-0.5.58.1/tools/snapshot_environment.py": b"x",
        "grandgousier-0.5.58.1/tools/acceptance_check.py": b"x",
        "grandgousier-0.5.58.1/release/README.md": b"x",
        "grandgousier-0.5.58.1/requirements-release.txt": b"x",
        "grandgousier-0.5.58.1/internal_tools/private.py": b"secret",
        "grandgousier-0.5.58.1/tools/check_m5_test_source_fields.py": b"run-specific",
    }
    with tarfile.open(sdist, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    checks = {row.name: row for row in release_check.inspect_sdist(sdist)}
    assert checks["sdist_private_paths"].status == "FAIL"
    assert checks["sdist_release_tools"].status == "OK"
    assert checks["sdist_run_specific_tools"].status == "FAIL"


def test_manifest_and_sdist_check_exclude_run_specific_checker(tmp_path):
    import io
    import tarfile

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    assert "exclude tools/check_m5_test_source_fields.py" in manifest

    sdist = tmp_path / "grandgousier-0.7.0.11.tar.gz"
    required = [
        "tools/release_check.py",
        "tools/snapshot_environment.py",
        "tools/acceptance_check.py",
        "release/README.md",
        "requirements-release.txt",
    ]
    with tarfile.open(sdist, "w:gz") as archive:
        for member in required:
            payload = b"x"
            info = tarfile.TarInfo(f"grandgousier-0.7.0.11/{member}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    checks = {
        row.name: row
        for row in release_check.inspect_sdist(sdist)
    }
    assert checks["sdist_run_specific_tools"].status == "OK"


def test_canonical_docs_do_not_require_omitted_private_context_files():
    canonical_docs = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "README_PUBLIC.md",
        ROOT / "docs" / "README.md",
        ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
        ROOT / "docs" / "ROADMAP.md",
    ]
    for path in canonical_docs:
        content = path.read_text(encoding="utf-8")
        assert "CONTEXT.md" not in content
        assert "context/project_state.yaml" not in content


def test_release_install_docs_use_github_release_wheel():
    wheel_url = (
        "https://github.com/NabatFasetochnii/Grandgousier/releases/download/v0.7.0.11/"
        "grandgousier-0.7.0.11-py3-none-any.whl"
    )
    assert wheel_url in (ROOT / "README_PUBLIC.md").read_text(encoding="utf-8")
    assert wheel_url in (ROOT / "release" / "README.md").read_text(encoding="utf-8")
    assert "python -m pip install grandgousier\n" not in (
        ROOT / "README_PUBLIC.md"
    ).read_text(encoding="utf-8")


def test_install_smoke_forces_target_isolation(monkeypatch, tmp_path):
    import subprocess

    calls = []

    def fake_run(cmd, *, cwd=release_check.ROOT, env=None):
        calls.append((list(cmd), Path(cwd)))
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="installed", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="version=0.5.58.1; origin=X; resource=Y\n",
            stderr="",
        )

    monkeypatch.setattr(release_check, "run", fake_run)
    result = release_check.install_smoke(tmp_path / "grandgousier-0.5.58.1-py3-none-any.whl", "0.5.58.1")

    assert result.status == "OK"
    pip_cmd = calls[0][0]
    smoke_cmd = calls[1][0]
    assert "--ignore-installed" in pip_cmd
    assert smoke_cmd[1:3] == ["-I", "-S"]
    compile(smoke_cmd[-1], "<wheel-install-smoke>", "exec")
    assert "import_origin_outside_target" in smoke_cmd[-1]
    assert "resource_outside_target" in smoke_cmd[-1]


def test_clean_build_staging_removes_stale_build_and_egg_info(monkeypatch, tmp_path):
    build = tmp_path / "build"
    egg = tmp_path / "grandgousier.egg-info"
    (build / "lib" / "grandgousier").mkdir(parents=True)
    (build / "lib" / "grandgousier" / "__init__.py").write_text(
        '__version__ = "0.5.58.1"\n', encoding="utf-8"
    )
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Version: 0.5.58.1\n", encoding="utf-8")

    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "BUILD", build)

    removed = release_check.clean_build_staging()

    assert build in removed
    assert egg in removed
    assert not build.exists()
    assert not egg.exists()


def test_build_release_cleans_stale_staging_before_backend(monkeypatch, tmp_path):
    import subprocess

    dist = tmp_path / "dist"
    build = tmp_path / "build"
    egg = tmp_path / "grandgousier.egg-info"
    build.mkdir()
    egg.mkdir()
    seen = {}

    def fake_run(cmd, *, cwd=release_check.ROOT, env=None):
        seen["build_exists"] = build.exists()
        seen["egg_exists"] = egg.exists()
        # build_release expects artifacts to exist after the backend call.
        dist.mkdir(exist_ok=True)
        (dist / "grandgousier-0.6.0.tar.gz").write_bytes(b"sdist")
        (dist / "grandgousier-0.6.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", build)
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda name: release_check.Version("82.0.1" if name == "setuptools" else "0.44"),
    )
    monkeypatch.setattr(release_check, "run", fake_run)

    checks, artifacts = release_check.build_release(clean=True)

    assert seen == {"build_exists": False, "egg_exists": False}
    assert checks[0].name == "BUILD_BACKEND_REQUIREMENTS_SATISFIED"
    assert checks[0].status == "OK"
    assert len(artifacts) == 2


def _write_project(
    path: Path,
    *,
    dependencies: tuple[str, ...] = ("alpha>=1",),
    optional: dict[str, tuple[str, ...]] | None = None,
    version: str = "1.2.3",
) -> None:
    dependency_lines = ",\n".join(json.dumps(item) for item in dependencies)
    optional_lines = []
    for group, values in (optional or {}).items():
        rendered = ",\n".join(json.dumps(item) for item in values)
        optional_lines.append(f"{json.dumps(group)} = [{rendered}]")
    path.write_text(
        "[project]\n"
        'name = "grandgousier"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{dependency_lines}]\n\n"
        "[project.optional-dependencies]\n"
        + "\n".join(optional_lines)
        + "\n",
        encoding="utf-8",
    )


def _write_build_system(
    path: Path,
    *,
    requires: tuple[str, ...] = ("setuptools>=68",),
    backend: str | None = "custom_backend.api:Builder",
) -> None:
    rendered_requires = ", ".join(json.dumps(value) for value in requires)
    backend_line = (
        f"build-backend = {json.dumps(backend)}\n" if backend is not None else ""
    )
    path.write_text(
        "[build-system]\n"
        f"requires = [{rendered_requires}]\n"
        + backend_line,
        encoding="utf-8",
    )


class _FakeDistribution:
    def __init__(self, name: str, requires: tuple[str, ...] = ()):
        self.metadata = {"Name": name}
        self.version = "1.0"
        self.requires = list(requires)

    def read_text(self, _name: str) -> None:
        return None


def _fake_distributions(monkeypatch, *distributions: _FakeDistribution) -> None:
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in distributions
    }
    monkeypatch.setattr(snapshot, "_installed_distributions", lambda: installed)


def _closure_names(closure) -> list[str]:
    return [row["canonical_name"] for row in closure.packages]


def test_dependency_closure_and_checker_use_only_selected_extra_context(
    monkeypatch, tmp_path
):
    cases = (
        (
            "root-extra",
            "foo[bar]",
            ('helper; extra == "bar"',),
            ["foo", "helper"],
        ),
        (
            "no-extra",
            "foo",
            ('helper; extra == "bar"',),
            ["foo"],
        ),
        (
            "negative-extra",
            "foo[binary]",
            (
                'helper; extra != "binary"',
                'ordinary; python_version >= "3.0"',
            ),
            ["foo", "ordinary"],
        ),
    )

    for name, root_requirement, requires, expected_names in cases:
        project_path = tmp_path / name / "pyproject.toml"
        project_path.parent.mkdir()
        _write_project(
            project_path,
            dependencies=(root_requirement,),
            version="1.2.3",
        )
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))
        _fake_distributions(
            monkeypatch,
            _FakeDistribution("foo", requires),
            _FakeDistribution("helper"),
            _FakeDistribution("ordinary"),
        )

        closure = snapshot.dependency_closure(project, ())
        assert _closure_names(closure) == expected_names
        assert closure.complete

        monkeypatch.setattr(release_check, "PYPROJECT", project_path)
        snapshot_path = project_path.parent / "environment_snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "format": "grandgousier_environment_snapshot_v2",
                    "grandgousier_version": "1.2.3",
                    "pyproject_sha256": release_check.sha256_file(project_path),
                    "selected_extras": [],
                    "missing_required_packages": closure.missing,
                    "package_count": len(closure.packages),
                    "packages": closure.packages,
                    "dependency_closure": {
                        "format": snapshot.DEPENDENCY_CLOSURE_FORMAT,
                        "complete": closure.complete,
                        "states": closure.states,
                        "unresolved_requirements": closure.unresolved_requirements,
                    },
                    "python": {"version": "3.11.0"},
                    "platform": {"system": "Windows"},
                }
            ),
            encoding="utf-8",
        )
        checked = release_check.check_snapshot(snapshot_path, version="1.2.3")
        assert checked.status == "OK", checked.detail


def test_dependency_closure_selects_root_optional_extra_only_when_requested(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution("alpha"),
        _FakeDistribution("beta"),
    )
    project = {
        "project": {
            "dependencies": ["alpha"],
            "optional-dependencies": {
                "fast": ['beta; extra == "fast"'],
            },
        }
    }

    core = snapshot.dependency_closure(project, ())
    with_extra = snapshot.dependency_closure(project, ("fast",))

    assert _closure_names(core) == ["alpha"]
    assert _closure_names(with_extra) == ["alpha", "beta"]
    assert with_extra.states == [
        {"canonical_name": "alpha", "extras": []},
        {"canonical_name": "beta", "extras": []},
    ]


def test_dependency_closure_evaluates_multiple_active_extras(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution(
            "alpha",
            (
                'first-dependency; extra == "first"',
                'second-dependency; extra == "second"',
            ),
        ),
        _FakeDistribution("first-dependency"),
        _FakeDistribution("second-dependency"),
    )
    project = {"project": {"dependencies": ["alpha[first,second]"]}}

    closure = snapshot.dependency_closure(project, ())

    assert _closure_names(closure) == ["alpha", "first-dependency", "second-dependency"]
    assert {tuple(state["extras"]) for state in closure.states if state["canonical_name"] == "alpha"} == {
        ("first", "second")
    }


def test_dependency_closure_propagates_dependency_extras(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution("root", ('middle[speed]>=2; python_version >= "3.0"',)),
        _FakeDistribution("middle", ('leaf; extra == "speed"',)),
        _FakeDistribution("leaf"),
    )
    project = {"project": {"dependencies": ["root"]}}

    closure = snapshot.dependency_closure(project, ())

    assert _closure_names(closure) == ["leaf", "middle", "root"]
    assert {tuple(state["extras"]) for state in closure.states if state["canonical_name"] == "middle"} == {
        ("speed",)
    }
    middle_edge = next(
        row for row in closure.packages if row["canonical_name"] == "root"
    )["requires"][0]
    assert middle_edge["extras"] == ["speed"]


def test_dependency_closure_evaluates_environment_and_extra_markers(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution(
            "alpha",
            (
                'active; python_version >= "3.0" and extra == "enabled"',
                'inactive; sys_platform == "no-such-platform" and extra == "enabled"',
            ),
        ),
        _FakeDistribution("active"),
        _FakeDistribution("inactive"),
    )
    project = {"project": {"dependencies": ["alpha[enabled]"]}}

    closure = snapshot.dependency_closure(project, ())

    assert _closure_names(closure) == ["active", "alpha"]


def test_dependency_closure_cycles_terminate_by_name_and_extras(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution("alpha", ("beta[feature]",)),
        _FakeDistribution("beta", ('alpha[feature]; extra == "feature"',)),
    )
    project = {"project": {"dependencies": ["alpha"]}}

    closure = snapshot.dependency_closure(project, ())

    assert _closure_names(closure) == ["alpha", "beta"]
    assert closure.states == [
        {"canonical_name": "alpha", "extras": []},
        {"canonical_name": "alpha", "extras": ["feature"]},
        {"canonical_name": "beta", "extras": ["feature"]},
    ]


def test_dependency_closure_reports_missing_distribution_and_incomplete_state(monkeypatch):
    _fake_distributions(monkeypatch, _FakeDistribution("present"))
    project = {"project": {"dependencies": ["present", "missing"]}}

    closure = snapshot.dependency_closure(project, ())

    assert closure.missing == ["missing"]
    assert not closure.complete
    assert closure.states == [
        {"canonical_name": "missing", "extras": []},
        {"canonical_name": "present", "extras": []},
    ]


def test_dependency_closure_output_is_deterministic_across_distribution_order(monkeypatch):
    alpha = _FakeDistribution("alpha", ("charlie>=1", "bravo>=1"))
    bravo = _FakeDistribution("bravo")
    charlie = _FakeDistribution("charlie")
    project = {"project": {"dependencies": ["alpha"]}}

    _fake_distributions(monkeypatch, alpha, bravo, charlie)
    first = snapshot.dependency_closure(project, ())
    _fake_distributions(monkeypatch, charlie, bravo, alpha)
    second = snapshot.dependency_closure(project, ())

    assert first == second


def test_dependency_closure_includes_psycopg_binary_extra_dependency(monkeypatch):
    _fake_distributions(
        monkeypatch,
        _FakeDistribution("psycopg", ('psycopg-binary>=3.1; extra == "binary"',)),
        _FakeDistribution("psycopg-binary"),
    )
    project = {"project": {"dependencies": ["psycopg[binary]>=3.1"]}}

    closure = snapshot.dependency_closure(project, ())

    assert _closure_names(closure) == ["psycopg", "psycopg-binary"]
    assert closure.complete
    psycopg = next(row for row in closure.packages if row["canonical_name"] == "psycopg")
    assert psycopg["requires"][0]["extras"] == []
    assert psycopg["requires"][0]["name"] == "psycopg-binary"


def test_check_snapshot_rejects_psycopg_snapshot_missing_binary_extra_dependency(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(
        project,
        dependencies=("psycopg[binary]>=3.1",),
        version="1.2.3",
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    snapshot_path = tmp_path / "environment_snapshot.json"
    payload = {
        "format": "grandgousier_environment_snapshot_v2",
        "grandgousier_version": "1.2.3",
        "pyproject_sha256": release_check.sha256_file(project),
        "selected_extras": [],
        "missing_required_packages": [],
        "package_count": 1,
        "packages": [
            {
                "name": "psycopg",
                "canonical_name": "psycopg",
                "version": "3.2.5",
                "requires": [
                    {
                        "name": "psycopg-binary",
                        "extras": [],
                        "specifier": ">=3.1",
                        "marker": 'extra == "binary"',
                        "url_present": False,
                    }
                ],
            }
        ],
        "dependency_closure": {
            "format": release_check.SNAPSHOT_CLOSURE_FORMAT,
            "complete": True,
            "states": [{"canonical_name": "psycopg", "extras": ["binary"]}],
            "unresolved_requirements": [],
        },
    }
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    result = release_check.check_snapshot(snapshot_path, version="1.2.3")

    assert result.status == "FAIL"
    assert "psycopg-binary" in result.detail


def test_build_requirements_fail_before_frontend_or_artifact_creation(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_build_system(project)
    dist = tmp_path / "dist"
    build = tmp_path / "build"
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", build)
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda name: release_check.Version("65.5") if name == "setuptools" else None,
    )
    calls = []
    monkeypatch.setattr(release_check, "run", lambda command, **kwargs: calls.append(command))

    checks, artifacts = release_check.build_release(clean=True)

    assert len(checks) == 1
    assert checks[0].name == "BUILD_BACKEND_REQUIREMENTS_SATISFIED"
    assert checks[0].status == "FAIL"
    assert "BUILD_BACKEND_REQUIREMENTS_SATISFIED=NO" in checks[0].detail
    assert "backend=custom_backend.api:Builder" in checks[0].detail
    assert 'declared_requires=["requires[0]=setuptools>=68"]' in checks[0].detail
    assert "setuptools=65.5" in checks[0].detail
    assert calls == []
    assert artifacts == []
    assert not dist.exists()
    assert not build.exists()


def test_compatible_build_requirements_invoke_configured_pep517_frontend(
    monkeypatch, tmp_path
):
    import subprocess

    project = tmp_path / "pyproject.toml"
    _write_build_system(project, requires=("setuptools>=68",))
    dist = tmp_path / "dist"
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", tmp_path / "build")
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda name: release_check.Version("82.0.1"),
    )
    calls = []

    def fake_run(command, *, cwd=release_check.ROOT, env=None):
        calls.append(list(command))
        (dist / "grandgousier-1.0.0.tar.gz").write_bytes(b"sdist")
        (dist / "grandgousier-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0, stdout="built", stderr="")

    monkeypatch.setattr(release_check, "run", fake_run)

    checks, artifacts = release_check.build_release(clean=False)

    assert checks[0].status == "OK"
    assert "backend=custom_backend.api:Builder" in checks[0].detail
    assert 'declared_requires=["requires[0]=setuptools>=68"]' in checks[0].detail
    assert "setuptools=82.0.1" in checks[0].detail
    assert calls == [[
        sys.executable,
        "-m",
        "build",
        "--sdist",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(dist),
    ]]
    assert len(artifacts) == 2


def test_build_requirement_exact_and_lower_bound_versions_use_pep440_semantics(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_build_system(
        project,
        requires=("exact-package==1.2.3", "lower-package>=1.2.3"),
    )
    versions = {"exact-package": "1.2.3", "lower-package": "1.2.4"}
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda name: release_check.Version(versions[name]),
    )

    assert release_check.check_build_backend_requirements().status == "OK"

    versions["exact-package"] = "1.2.4"
    versions["lower-package"] = "1.2.2"
    result = release_check.check_build_backend_requirements()
    assert result.status == "FAIL"
    assert "exact-package==1.2.3 is incompatible with installed 1.2.4" in result.detail
    assert "lower-package>=1.2.3 is incompatible with installed 1.2.2" in result.detail


def test_missing_build_distribution_fails_closed(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_build_system(project, requires=("missing-builder>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)

    def missing_distribution(_name):
        raise release_check.importlib.metadata.PackageNotFoundError("missing-builder")

    monkeypatch.setattr(release_check, "_installed_distribution_version", missing_distribution)
    result = release_check.check_build_backend_requirements()
    assert result.status == "FAIL"
    assert "BUILD_BACKEND_REQUIREMENTS_SATISFIED=NO" in result.detail
    assert "missing-builder=<missing>" in result.detail


def test_malformed_build_requirement_fails_closed(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    raw_requirement = "not a valid requirement with secret-token ???"
    _write_build_system(project, requires=(raw_requirement,))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not inspect malformed requirement")),
    )
    real_requirement = release_check.Requirement

    def reject_with_echo(value):
        if value == raw_requirement:
            raise ValueError(f"parser details echo {value}")
        return real_requirement(value)

    monkeypatch.setattr(release_check, "Requirement", reject_with_echo)

    result = release_check.check_build_backend_requirements()
    assert result.status == "FAIL"
    assert "BUILD_BACKEND_REQUIREMENTS_SATISFIED=NO" in result.detail
    assert "is malformed" in result.detail
    assert "<malformed requirement>" in result.detail
    assert raw_requirement not in result.detail
    assert "secret-token" not in result.detail
    assert "parser details echo" not in result.detail


def test_build_requirement_markers_are_evaluated(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_build_system(
        project,
        requires=(
            'active-builder>=1; python_version >= "3.0"',
            'inactive-builder>=1; sys_platform == "no-such-platform"',
        ),
    )
    seen = []

    def installed_version(name):
        seen.append(name)
        return release_check.Version("1.0")

    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "_installed_distribution_version", installed_version)

    result = release_check.check_build_backend_requirements()

    assert result.status == "OK"
    assert seen == ["active-builder"]
    assert "inactive-builder=<skipped: marker false>" in result.detail


def test_invalid_or_missing_build_backend_fails_before_frontend(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda _name: release_check.Version("1.0"),
    )
    calls = []
    monkeypatch.setattr(release_check, "run", lambda command, **kwargs: calls.append(command))

    for backend in (None, "not a backend"):
        _write_build_system(project, backend=backend)
        result, artifacts = release_check.build_release(clean=True)
        assert result[0].status == "FAIL"
        assert "invalid or missing build-backend" in result[0].detail
        expected_backend = "backend=<missing>" if backend is None else "backend='not a backend'"
        assert expected_backend in result[0].detail
        assert artifacts == []

    assert calls == []
    assert not (tmp_path / "dist").exists()


def test_frontend_failure_for_unsupported_backend_has_no_fallback(monkeypatch, tmp_path):
    import subprocess

    project = tmp_path / "pyproject.toml"
    _write_build_system(project, backend="unsupported_backend:build")
    dist = tmp_path / "dist"
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", tmp_path / "build")
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda _name: release_check.Version("82.0.1"),
    )
    calls = []

    def frontend_failure(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="unsupported backend"
        )

    monkeypatch.setattr(release_check, "run", frontend_failure)

    checks, artifacts = release_check.build_release(clean=False)

    assert checks[0].status == "OK"
    assert checks[1].name == "build"
    assert checks[1].status == "FAIL"
    assert "unsupported backend" in checks[1].detail
    assert len(calls) == 1
    assert calls[0][1:3] == ["-m", "build"]
    assert "-c" not in calls[0]
    assert artifacts == []


def test_main_reports_incompatible_build_requirements_as_failed_without_artifacts(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_build_system(project, requires=("setuptools>=68",))
    dist = tmp_path / "dist"
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", tmp_path / "build")
    monkeypatch.setattr(release_check, "read_versions", lambda: ("1.0.0", "1.0.0"))
    monkeypatch.setattr(
        release_check,
        "_installed_distribution_version",
        lambda _name: release_check.Version("65.5"),
    )
    calls = []
    monkeypatch.setattr(release_check, "run", lambda command, **kwargs: calls.append(command))

    assert release_check.main(["--report", "release-report.json"]) == 1

    report = json.loads((tmp_path / "release-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAILED"
    requirement_check = next(
        row
        for row in report["checks"]
        if row["name"] == "BUILD_BACKEND_REQUIREMENTS_SATISFIED"
    )
    assert requirement_check["status"] == "FAIL"
    assert "BUILD_BACKEND_REQUIREMENTS_SATISFIED=NO" in requirement_check["detail"]
    assert calls == []
    assert report["artifacts"] == []
    assert not list(dist.glob("*.whl"))
    assert not list(dist.glob("*.tar.gz"))


def test_direct_url_build_requirement_is_unverifiable_and_rejected(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_build_system(
        project,
        requires=("builder @ https://username:secret-token@example.invalid/builder.whl",),
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)

    result = release_check.check_build_backend_requirements()

    assert result.status == "FAIL"
    assert "unsupported direct reference" in result.detail
    assert "builder=<unverifiable direct reference>" in result.detail
    assert 'declared_requires=["requires[0]=builder @ <direct reference>"]' in result.detail
    assert "username" not in result.detail
    assert "secret-token" not in result.detail


def _metadata_text(
    *,
    requires: tuple[str, ...] = ("alpha>=1",),
    extras: tuple[str, ...] = (),
    version: str = "1.2.3",
    name: str = "grandgousier",
    requires_python: str = ">=3.11",
    metadata_version: str = "2.4",
) -> str:
    lines = [
        f"Metadata-Version: {metadata_version}",
        f"Name: {name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    lines.extend(f"Requires-Dist: {value}" for value in requires)
    lines.extend(f"Provides-Extra: {value}" for value in extras)
    return "\n".join(lines) + "\n\n"


def _metadata_for_project(path: Path) -> str:
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Requires-Python: {project['requires-python']}",
    ]
    lines.extend(f"Requires-Dist: {Requirement(raw)}" for raw in project["dependencies"])
    for group, values in project.get("optional-dependencies", {}).items():
        normalized_group = canonicalize_name(group)
        for raw in values:
            requirement = Requirement(raw)
            base = str(requirement).split(";", 1)[0].strip()
            marker = (
                Marker(f"({requirement.marker}) and extra == {normalized_group!r}")
                if requirement.marker
                else Marker(f"extra == {normalized_group!r}")
            )
            lines.append(f"Requires-Dist: {base}; {marker}")
        lines.append(f"Provides-Extra: {group}")
    return "\n".join(lines) + "\n\n"


def _write_wheel(path: Path, metadata: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for resource in release_check.REQUIRED_WHEEL_RESOURCES:
            archive.writestr(resource, "x")
        archive.writestr("grandgousier-1.2.3.dist-info/METADATA", metadata)
    return path


def _write_sdist(
    path: Path,
    metadata_copies: tuple[bytes, ...],
    *,
    root: str = "grandgousier-1.2.3",
    extra_members: tuple[str, ...] = (),
) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in extra_members:
            info = tarfile.TarInfo(member)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        for index, payload in enumerate(metadata_copies):
            member = f"{root}/PKG-INFO"
            if index:
                member = f"{root}/copy-{index}/PKG-INFO"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _checks(rows):
    return {row.name: row for row in rows}


def test_metadata_gate_reports_source_26_wheel_26_sdist_zero(monkeypatch, tmp_path):
    source = release_check.read_project_metadata()
    assert len(source.requires_dist) == 26
    assert len(source.provides_extra) == 5
    assert source.version == "0.7.0.11"

    wheel = _write_wheel(
        tmp_path / "grandgousier-0.7.0.11-py3-none-any.whl",
        _metadata_for_project(release_check.PYPROJECT),
    )
    wheel_checks = _checks(release_check.inspect_wheel(wheel, source.version))
    assert wheel_checks["wheel_dependencies_match_source"].status == "OK"
    assert wheel_checks["wheel_extras_match_source"].status == "OK"
    assert wheel_checks["wheel_dependencies_match_source"].detail == "source=26,wheel=26"

    zero_dependency_metadata = _metadata_text(
        requires=(),
        version=source.version,
        extras=tuple(sorted(source.provides_extra)),
    ).encode()
    sdist = _write_sdist(
        tmp_path / "grandgousier-0.7.0.11.tar.gz",
        (zero_dependency_metadata, zero_dependency_metadata),
        root=f"grandgousier-{source.version}",
    )
    sdist_checks = _checks(release_check.inspect_sdist(sdist, version=source.version))
    assert sdist_checks["sdist_metadata_copies_consistent"].status == "OK"
    assert sdist_checks["sdist_dependencies_match_source"].status == "FAIL"
    assert sdist_checks["sdist_dependencies_match_source"].detail == (
        "source=26,sdist metadata differs: "
        "grandgousier-0.7.0.11/PKG-INFO=0,"
        "grandgousier-0.7.0.11/copy-1/PKG-INFO=0"
    )
    assert sdist_checks["sdist_extras_match_source"].status == "OK"
    assert sdist_checks["sdist_metadata_version"].status == "OK"


def test_wheel_private_path_gate_rejects_git_and_opencode_members(tmp_path):
    wheel = _write_wheel(tmp_path / "test.whl", _metadata_text())
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(".git/config", "x")
        archive.writestr("grandgousier/.opencode/settings.json", "x")

    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_private_paths"].status == "FAIL"
    assert ".git/config" in checks["wheel_private_paths"].detail
    assert "grandgousier/.opencode/settings.json" in checks["wheel_private_paths"].detail


def test_sdist_private_path_gate_rejects_git_and_opencode_members(tmp_path):
    sdist = _write_sdist(
        tmp_path / "test.tar.gz",
        (),
        extra_members=(
            "grandgousier-1.2.3/.git/config",
            "grandgousier-1.2.3/package/.opencode/settings.json",
        ),
    )

    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_private_paths"].status == "FAIL"
    assert "grandgousier-1.2.3/.git/config" in checks["sdist_private_paths"].detail
    assert "grandgousier-1.2.3/package/.opencode/settings.json" in (
        checks["sdist_private_paths"].detail
    )


def test_wheel_metadata_normalizes_pep508_names_specifiers_markers_and_extras(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(
        project,
        dependencies=("Foo_Bar[Feature_X]>=1.0,<2; python_version >= '3.10'",),
        optional={"Fast_Mode": ("Baz>=2",)},
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=(
            'foo-bar[feature-x]<2,>=1.0; python_version>="3.10"',
            'baz>=2; extra == "FAST_mode"',
        ),
        extras=("fast_mode",),
    )
    wheel = _write_wheel(tmp_path / "test.whl", metadata)
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"
    assert checks["wheel_extras_match_source"].status == "OK"


def test_wheel_dependency_mismatch_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(requires=("alpha>=2",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_trailing_zero_specifier_versions_compare_semantically(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1.0",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(requires=("alpha>=1.0.0",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_wheel_changed_marker_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1; python_version >= '3.10'",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1; python_version >= '3.11'",)),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_reordered_markers_match_with_optional_extra_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(
        project,
        optional={
            "Fast_Mode": (
                "Baz>=2; python_version >= '3.10' or sys_platform == 'win32'",
            )
        },
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(
            requires=(
                "alpha>=1",
                'baz>=2; extra == "FAST_mode" and '
                '(sys_platform == "win32" or python_version >= "3.10")',
            ),
            extras=("fast_mode",),
        ),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"
    assert checks["wheel_extras_match_source"].status == "OK"


def test_wheel_unexpected_dependency_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1", "surprise>=1")),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_missing_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1", 'beta>=1; extra == "fast"')),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_extras_match_source"].status == "FAIL"
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_wheel_unexpected_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(extras=("other",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_extras_match_source"].status == "FAIL"
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_sdist_metadata_matches_source_dependencies_and_extras(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("fast",)
    )
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "OK"
    assert checks["sdist_extras_match_source"].status == "OK"
    assert checks["sdist_metadata_version"].status == "OK"


def test_sdist_missing_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=("alpha>=1", 'beta>=1; extra == "fast"')
    )
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "OK"
    assert checks["sdist_extras_match_source"].status == "FAIL"


def test_sdist_unexpected_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(extras=("other",))
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_extras_match_source"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "OK"


def test_sdist_unexpected_dependency_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(requires=("alpha>=1", "surprise>=1"))
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "FAIL"
    assert checks["sdist_extras_match_source"].status == "OK"


def test_sdist_without_pkg_info_fails_metadata_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    sdist = _write_sdist(tmp_path / "test.tar.gz", ())
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"
    assert checks["sdist_extras_match_source"].status == "FAIL"


def test_malformed_sdist_and_wheel_metadata_fail_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (b"not valid core metadata\n\n",))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"

    for index, metadata in enumerate((None, "not valid core metadata\n\n")):
        wheel = tmp_path / f"bad-{index}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            if metadata is not None:
                archive.writestr("grandgousier-1.2.3.dist-info/METADATA", metadata)
        wheel_checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
        assert wheel_checks["wheel_metadata"].status == "FAIL"
        assert wheel_checks["wheel_dependencies_match_source"].status == "FAIL"


def test_two_equivalent_sdist_pkg_info_copies_are_allowed(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1,<2",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text(requires=("alpha>=1,<2",)).encode()
    second = (
        "Version: 1.2.3\n"
        "Requires-Python: >=3.11\n"
        "Name: grandgousier\n"
        "Metadata-Version: 2.4\n"
        "Requires-Dist: alpha<2,>=1\n\n"
    ).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "OK"
    assert checks["sdist_dependencies_match_source"].status == "OK"


def test_inconsistent_sdist_pkg_info_copies_fail_consistency(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text().encode()
    second = _metadata_text(requires=("alpha>=2",)).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"


def test_one_sdist_pkg_info_copy_missing_dependencies_fails_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text(requires=("alpha>=1",)).encode()
    second = _metadata_text(requires=()).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"


def test_wheel_sdist_dependency_mismatch_fails_cross_artifact_gate(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(tmp_path / "test.whl", _metadata_text())
    sdist = _write_sdist(
        tmp_path / "test.tar.gz", (_metadata_text(requires=("alpha>=2",)).encode(),)
    )
    checks = _checks(release_check.compare_wheel_sdist_metadata(wheel, sdist))
    assert checks["wheel_sdist_dependencies_match"].status == "FAIL"
    assert checks["wheel_sdist_extras_match"].status == "OK"


def test_wheel_sdist_extra_mismatch_fails_cross_artifact_gate(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel_metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("fast",)
    )
    sdist_metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("other",)
    )
    wheel = _write_wheel(tmp_path / "test.whl", wheel_metadata)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (sdist_metadata.encode(),))
    checks = _checks(release_check.compare_wheel_sdist_metadata(wheel, sdist))
    assert checks["wheel_sdist_extras_match"].status == "FAIL"


def test_metadata_distribution_version_mismatch_fails_wheel_and_sdist_checks(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wrong = _metadata_text(version="1.2.4")
    wheel = _write_wheel(tmp_path / "test.whl", wrong)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (wrong.encode(),))
    wheel_checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    sdist_checks = _checks(release_check.inspect_sdist(sdist))
    assert wheel_checks["wheel_metadata"].status == "FAIL"
    assert sdist_checks["sdist_metadata_version"].status == "FAIL"


def test_verify_existing_checks_exact_artifacts_without_touching_artifacts_or_staging(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    root = tmp_path
    dist_dir = root / "dist"
    dist_dir.mkdir()
    wheel = _write_wheel(
        dist_dir / "grandgousier-1.2.3-py3-none-any.whl",
        _metadata_for_project(project),
    )
    metadata = _metadata_for_project(project).encode()
    sdist = _write_sdist(
        dist_dir / "grandgousier-1.2.3.tar.gz",
        (metadata,),
        extra_members=(
            "grandgousier-1.2.3/tools/release_check.py",
            "grandgousier-1.2.3/tools/snapshot_environment.py",
            "grandgousier-1.2.3/tools/acceptance_check.py",
            "grandgousier-1.2.3/release/README.md",
            "grandgousier-1.2.3/requirements-release.txt",
        ),
    )
    staging = root / "build" / "lib" / "grandgousier"
    staging.mkdir(parents=True)
    staged_file = staging / "stale.py"
    staged_file.write_bytes(b"staging must remain untouched")
    preserved = [wheel, sdist, staged_file]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in preserved
    }

    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    monkeypatch.setattr(release_check, "DIST", dist_dir)
    monkeypatch.setattr(release_check, "BUILD", root / "build")
    monkeypatch.setattr(release_check, "read_versions", lambda: ("1.2.3", "1.2.3"))
    monkeypatch.setattr(
        release_check,
        "install_smoke",
        lambda _wheel, _version: release_check.Check("wheel_install_smoke", "OK", "stubbed"),
    )

    def unexpected_action(*_args, **_kwargs):
        raise AssertionError("verify-existing must not build, clean, or write checksums")

    monkeypatch.setattr(release_check, "build_release", unexpected_action)
    monkeypatch.setattr(release_check, "clean_build_staging", unexpected_action)
    monkeypatch.setattr(release_check, "write_checksums", unexpected_action)
    report = root / "verify-existing.json"

    result = release_check.main(
        [
            "--verify-existing",
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--report",
            str(report),
        ]
    )

    assert result == 0
    report_data = json.loads(report.read_text(encoding="utf-8"))
    assert report_data["mode"] == "verify-existing"
    assert report_data["status"] == "READY_WITH_WARNINGS"
    hashes = {Path(row["path"]).name: row["sha256"] for row in report_data["artifacts"]}
    assert hashes[wheel.name] == release_check.sha256_file(wheel)
    assert hashes[sdist.name] == release_check.sha256_file(sdist)
    assert all(
        (path.read_bytes(), path.stat().st_mtime_ns) == before[path]
        for path in preserved
    )


def test_verify_existing_refuses_existing_report_without_overwriting_it(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    report = tmp_path / "existing-report.json"
    report.write_bytes(b"review evidence must stay unchanged\n")
    before = (report.read_bytes(), report.stat().st_mtime_ns)

    with pytest.raises(SystemExit) as error:
        release_check.main(
            [
                "--verify-existing",
                "--wheel",
                str(tmp_path / "artifact.whl"),
                "--sdist",
                str(tmp_path / "artifact.tar.gz"),
                "--report",
                str(report),
            ]
        )

    assert error.value.code == 2
    assert (report.read_bytes(), report.stat().st_mtime_ns) == before
