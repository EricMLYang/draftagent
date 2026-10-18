"""build() API 與 `draftagent` CLI：kinds / schema / new / build / verify / preview / 友善錯誤。"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import ezdxf
import pytest
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from typer.testing import CliRunner

from draftagent import BuildFailed, PlanSpec, Rect, Room, build, load_spec, registry
from draftagent.cli import app
from draftagent.codegen import spec_to_python
from draftagent.errors import SpecLoadError, format_validation_error
from draftagent.registry import Kind
from draftagent.units import DXF_INSUNITS, check_insunits
from draftagent.verify.base import VerifyResult

ROOT = Path(__file__).resolve().parents[1]
PLAN_EXAMPLE = ROOT / "tests/level1/1-1/input/spec.json"
KIND_NAMES = [k.name for k in registry.all_kinds()]
runner = CliRunner()


def cli(*args: str):
    return runner.invoke(app, [str(a) for a in args])


@pytest.fixture
def workdir() -> Iterator[Path]:
    """repo 內的暫存 work/ 子目錄（.py spec 必須在 repo 內）。"""
    d = ROOT / "work" / f"_pytest_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _one_run(root: Path) -> Path:
    runs = [p for p in root.iterdir() if p.is_dir()]
    assert len(runs) == 1, runs
    return runs[0]


# ---------------------------------------------------------------- registry / kinds / schema


def test_builtin_kinds_registered():
    for name in ("plan", "site-overlay", "slope", "drainage-calc-ty", "drainage-profile"):
        k = registry.get(name)
        assert k.example is not None and k.example.exists()
        assert k.verifiers


def test_cli_kinds_text_and_json():
    r = cli("kinds")
    assert r.exit_code == 0, r.output
    assert "plan" in r.output and "examples/plan.json" in r.output
    r = cli("kinds", "--json")
    assert r.exit_code == 0
    rows = json.loads(r.output)
    assert {row["name"] for row in rows} >= {"plan", "drainage-profile"}
    assert all(row["summary"] and row["example"] for row in rows)


def test_cli_schema():
    r = cli("schema", "plan")
    assert r.exit_code == 0
    s = json.loads(r.output)
    assert s["title"] == "PlanSpec" and "wall_thickness" in s["properties"]


def test_cli_schema_unknown_kind():
    r = cli("schema", "nope")
    assert r.exit_code == 1
    assert "未知圖種" in r.output and "plan" in r.output
    r = cli("schema", "nope", "--json")
    assert r.exit_code == 1 and json.loads(r.output)["ok"] is False


# ---------------------------------------------------------------- new / codegen


@pytest.mark.parametrize("name", KIND_NAMES)
def test_new_roundtrips_example(name, workdir):
    k = registry.get(name)
    out = workdir / f"{name.replace('-', '_')}.py"
    r = cli("new", name, "-o", out)
    assert r.exit_code == 0, r.output
    code = out.read_text(encoding="utf-8")
    assert "spec = " in code and "draftagent build" in code
    compile(code, str(out), "exec")
    got = load_spec(out)
    want = load_spec(k.example, kind=name)
    assert type(got) is type(want)
    assert got.model_dump(mode="json") == want.model_dump(mode="json")


def test_new_stdout_and_no_overwrite(workdir):
    r = cli("new", "plan")
    assert r.exit_code == 0 and "PlanSpec(" in r.output
    out = workdir / "p.py"
    assert cli("new", "plan", "-o", out).exit_code == 0
    r = cli("new", "plan", "-o", out)
    assert r.exit_code == 1 and "--force" in r.output
    assert cli("new", "plan", "-o", out, "--force").exit_code == 0


def test_codegen_omits_unset_defaults():
    spec = PlanSpec(wall_thickness=150, rooms=[Room(name="A", rect=Rect(width=3000, depth=2500))])
    code = spec_to_python(spec, kind="plan")
    assert "dimensions=" not in code and "openings=" not in code
    ns: dict = {}
    exec(compile(code, "<gen>", "exec"), ns)
    assert ns["spec"].model_dump() == spec.model_dump()


# ---------------------------------------------------------------- load_spec


def test_load_spec_json_yaml_py(tmp_path, workdir):
    want = load_spec(PLAN_EXAMPLE)
    assert isinstance(want, PlanSpec)

    y = tmp_path / "spec.yaml"
    y.write_text(yaml.safe_dump(json.loads(PLAN_EXAMPLE.read_text("utf-8")), allow_unicode=True),
                 encoding="utf-8")
    assert load_spec(y) == want

    py = workdir / "s.py"
    py.write_text(
        "from draftagent import PlanSpec, Room, Rect\n"
        "def spec():\n"
        "    return PlanSpec(wall_thickness=200, rooms=[Room(name='x', rect=Rect(width=5000, depth=3000))])\n"
        "raise_if_main = __name__ == '__main__'\n"
        "assert not raise_if_main\n",
        encoding="utf-8",
    )
    s = load_spec(py)
    assert isinstance(s, PlanSpec) and s.rooms[0].name == "x"


def test_load_spec_guesses_kind_without_kind_field():
    s = load_spec(ROOT / "tests/level2/drainage-ty-1/input/spec.json")
    assert registry.for_spec(s).name == "drainage-calc-ty"
    s = load_spec(ROOT / "tests/level1/1-2/input/spec.json", kind="site-overlay")
    assert registry.for_spec(s).name == "site-overlay"


def test_load_spec_kind_alias(tmp_path):
    # JSON 的 kind 可寫 spec 的 literal（"profile"）或 registry 名（"drainage-profile"）
    src = ROOT / "tests/level2/profile-ty-1/input/spec.json"
    assert registry.for_spec(load_spec(src)).name == "drainage-profile"
    data = json.loads(src.read_text("utf-8"))
    data["kind"] = "drainage-profile"
    p = tmp_path / "p.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_spec(p).kind == "profile"
    # 沒有 kind 欄位的 spec（SiteSpec）也可在檔案裡標 kind
    site = json.loads((ROOT / "tests/level1/1-2/input/spec.json").read_text("utf-8"))
    site["kind"] = "site-overlay"
    p.write_text(json.dumps(site, ensure_ascii=False), encoding="utf-8")
    assert registry.for_spec(load_spec(p)).name == "site-overlay"


def test_load_spec_errors(tmp_path, workdir):
    outside = tmp_path / "s.py"
    outside.write_text("spec = 1\n", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="repo 內"):
        load_spec(outside)
    bad = workdir / "nospec.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="spec"):
        load_spec(bad)
    j = tmp_path / "broken.json"
    j.write_text("{oops", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="JSON 格式錯誤"):
        load_spec(j)
    with pytest.raises(SpecLoadError, match="找不到"):
        load_spec(tmp_path / "missing.json")
    with pytest.raises(SpecLoadError, match="不符"):
        load_spec(_write_plan_py(workdir), kind="slope")


def _write_plan_py(d: Path) -> Path:
    p = d / "plan_ok.py"
    p.write_text(
        "from draftagent import PlanSpec, Room, Rect\n"
        "spec = PlanSpec(wall_thickness=200, rooms=[Room(name='x', rect=Rect(width=5000, depth=3000))])\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------- build


@pytest.mark.parametrize("name", KIND_NAMES)
def test_build_each_kind_example(name, tmp_path):
    k = registry.get(name)
    r = cli("build", k.example, "--kind", name, "--out", tmp_path, "--no-preview", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["ok"] is True and out["kind"] == name
    run = _one_run(tmp_path)
    assert run.name.endswith(f"-{name}")
    for f in ("spec.json", "manifest.json", "verify.json", "verify.txt"):
        assert (run / f).exists(), f
    man = json.loads((run / "manifest.json").read_text("utf-8"))
    assert man["kind"] == name and man["ok"] is True and man["status"] == "done"
    assert man["draftagent_version"] and man["schema_sha256"]
    assert "commit" in man["git"]
    ver = json.loads((run / "verify.json").read_text("utf-8"))
    assert ver["ok"] is True and len(ver["results"]) == len(k.verifiers)
    # spec.json 可 replay：讀回後與原 spec 相同
    assert load_spec(run / "spec.json", kind=name).model_dump() == \
        load_spec(k.example, kind=name).model_dump()


def test_build_api_writes_run_dir_and_preview(tmp_path):
    spec = PlanSpec(
        title="測試", wall_thickness=200, rooms=[Room(name="房", rect=Rect(width=6000, depth=4000))]
    )
    res = build(spec, name="demo 1", out=tmp_path)
    assert res.ok, str(res)
    assert res.run_dir.parent == tmp_path and res.run_dir.name.endswith("-plan-demo-1")
    assert {"dxf", "spec", "png"} <= set(res.artifacts)
    assert res.artifacts["png"].exists()
    text = str(res)
    assert "[dxf_check]" in text and "[units_check]" in text and "RESULT: PASS" in text
    assert (res.run_dir / "verify.txt").read_text("utf-8").strip() == res.report().strip()
    saved = json.loads((res.run_dir / "spec.json").read_text("utf-8"))
    assert saved["title"] == "測試"  # ensure_ascii=False
    assert "測試" in (res.run_dir / "spec.json").read_text("utf-8")
    assert res.raise_for_status() is res


def test_cli_build_py_with_preview(tmp_path, workdir):
    py = workdir / "myplan.py"
    assert cli("new", "plan", "-o", py).exit_code == 0
    r = cli("build", py, "--out", tmp_path)
    assert r.exit_code == 0, r.output
    assert "[dxf_check]" in r.output and "RESULT: PASS" in r.output
    run = _one_run(tmp_path)
    assert run.name.endswith("-plan-myplan")
    assert (run / "plan.png").exists() and (run / "plan.dxf").exists()


# ---------------------------------------------------------------- red paths (fake kind)


class _FakeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "fake"
    boom: bool = False
    red: bool = False


def _fake_render(spec: _FakeSpec, out: Path) -> dict[str, Path]:
    if spec.boom:
        raise ValueError("render 爆了")
    p = out / "x.txt"
    p.write_text("x", encoding="utf-8")
    return {"txt": p}


def _fake_verify(spec: _FakeSpec, artifacts: dict[str, Path]) -> VerifyResult:
    res = VerifyResult(name="fake_check")
    if spec.red:
        res.error("故意紅燈")
    return res


def _crash_verify(spec, artifacts):
    raise RuntimeError("verifier 崩潰")


@pytest.fixture
def fake_kind(monkeypatch):
    k = Kind(name="fake", spec=_FakeSpec, render=_fake_render, verifiers=(_fake_verify,),
             summary="test")
    monkeypatch.setitem(registry._KINDS, "fake", k)
    return k


def test_build_red_and_raise_for_status(fake_kind, tmp_path):
    res = build(_FakeSpec(red=True), out=tmp_path, preview=False)
    assert not res.ok and "故意紅燈" in str(res) and "RESULT: FAIL" in str(res)
    with pytest.raises(BuildFailed, match="故意紅燈"):
        res.raise_for_status()
    assert json.loads((res.run_dir / "verify.json").read_text("utf-8"))["ok"] is False


def test_build_render_exception_becomes_red(fake_kind, tmp_path):
    res = build(_FakeSpec(boom=True), out=tmp_path)
    assert not res.ok and res.results[0].name == "render"
    assert "render 爆了" in res.results[0].errors[0]
    assert (res.run_dir / "render_error.txt").exists()
    assert (res.run_dir / "spec.json").exists()


def test_build_verifier_exception_becomes_red(fake_kind, tmp_path, monkeypatch):
    monkeypatch.setitem(
        registry._KINDS, "fake",
        Kind(name="fake", spec=_FakeSpec, render=_fake_render,
             verifiers=(_fake_verify, _crash_verify), summary="t"),
    )
    res = build(_FakeSpec(), out=tmp_path)
    assert not res.ok and "verifier 崩潰" in res.results[1].errors[0]


def test_run_dir_names_do_not_collide(fake_kind, tmp_path):
    a = build(_FakeSpec(), out=tmp_path)
    b = build(_FakeSpec(), out=tmp_path)
    assert a.run_dir != b.run_dir


# ---------------------------------------------------------------- verify / preview


def test_cli_verify_existing_artifacts(tmp_path):
    res = build(load_spec(PLAN_EXAMPLE), out=tmp_path, preview=False)
    assert res.ok
    r = cli("verify", "plan", res.run_dir / "spec.json", "--artifacts", res.run_dir)
    assert r.exit_code == 0, r.output
    assert "[dxf_check]" in r.output and "RESULT: PASS" in r.output
    # spec 改了（多一扇門）但產物沒重畫 → verifier 必須紅燈
    data = json.loads(PLAN_EXAMPLE.read_text("utf-8"))
    data["openings"].append({"type": "door", "side": "N", "offset": 1000, "width": 900})
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = cli("verify", "plan", changed, "--artifacts", res.run_dir, "--json")
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out["ok"] is False and any("門數不符" in e for x in out["results"] for e in x["errors"])
    # 產物不見 → 紅燈
    (res.run_dir / "plan.dxf").unlink()
    r = cli("verify", "plan", res.run_dir / "spec.json", "--artifacts", res.run_dir)
    assert r.exit_code == 1


def test_cli_preview(tmp_path):
    res = build(load_spec(PLAN_EXAMPLE), out=tmp_path, preview=False)
    png = tmp_path / "p.png"
    r = cli("preview", res.artifacts["dxf"], "-o", png, "--dpi", "50")
    assert r.exit_code == 0, r.output
    assert png.exists() and png.stat().st_size > 0
    assert cli("preview", tmp_path / "nope.dxf").exit_code == 1


# ---------------------------------------------------------------- friendly errors


def test_friendly_validation_error_cli(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "kind": "plan", "units": "cm", "wall_thickness": -5,
        "rooms": [{"name": "A", "rect": {"width": 6000, "depht": 4000}}],
    }), encoding="utf-8")
    r = cli("build", bad, "--out", tmp_path / "runs")
    assert r.exit_code == 1
    out = r.output
    assert "Traceback" not in out
    assert "PlanSpec 驗證失敗" in out
    assert "rooms[0].rect.depth" in out and "必填" in out
    assert "rooms[0].rect.depht" in out and "允許：x, y, width, depth" in out
    assert "範圍：> 0" in out and "允許值：'mm'" in out
    assert not (tmp_path / "runs").exists()  # spec 不合法就不建 run 目錄
    r = cli("build", bad, "--json")
    d = json.loads(r.output)
    assert d["ok"] is False and {e["loc"] for e in d["details"]} >= {"units", "wall_thickness"}


def test_friendly_validation_error_from_py(workdir):
    py = workdir / "bad.py"
    py.write_text(
        "from draftagent import PlanSpec, Room, Rect\n"
        "spec = PlanSpec(wall_thickness=200, rooms=[Room(name='x', rect=Rect(width=-1, depth=3000))])\n",
        encoding="utf-8",
    )
    r = cli("build", py)
    assert r.exit_code == 1 and "Traceback" not in r.output
    assert "Rect 驗證失敗" in r.output and "width" in r.output and "範圍：> 0" in r.output


def test_format_validation_error_model_validator_message():
    with pytest.raises(ValidationError) as ei:
        PlanSpec.model_validate({"wall_thickness": 200, "rooms": [{"name": "A"}]})
    msg = format_validation_error(ei.value)
    assert "rooms[0]" in msg and "rect 與 polygon" in msg and "Value error" not in msg


def test_unknown_json_kind_suggests_closest(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"foo": 1}', encoding="utf-8")
    r = cli("build", p)
    assert r.exit_code == 1 and "無法判斷圖種" in r.output and "--kind" in r.output


# ---------------------------------------------------------------- units / check


def test_check_insunits():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = DXF_INSUNITS["mm"]
    assert check_insunits(doc, "mm").ok
    bad = check_insunits(doc, "m")
    assert not bad.ok and "應為 6" in bad.errors[0]
    assert not check_insunits(doc, "cm").ok


def test_cli_check_json():
    r = cli("check", "--json")
    env = json.loads(r.output)
    assert r.exit_code == (0 if env["ok"] else 1)
    assert env["packages"]["ezdxf"]["ok"]
    assert {"pyswmm", "swmm-toolkit", "ODA File Converter"} <= set(env["optional"])
    assert set(KIND_NAMES) <= set(env["kinds"])
