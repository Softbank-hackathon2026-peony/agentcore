"""Standard generation and safe minimal repair preserve the checked module contract."""
import pytest

from agent import config, terraform
from agent.brain import StrandsBrain
from agent.schemas import TerraformEdit, TerraformFix, TerraformPatch, TfFile
from agent.storage import LocalStore
from tests.fakes import reference_files


@pytest.mark.parametrize("arch", ["ec2", "lambda", "cloud_run"])
def test_standard_generation_never_calls_model_and_preserves_reference(arch, monkeypatch):
    monkeypatch.setattr(config, "TF_USE_REFERENCE", True)
    brain = StrandsBrain()
    monkeypatch.setattr(brain, "_tf_agent", lambda *args: pytest.fail("Standard generation called Bedrock"))
    out = brain.gen_terraform({"container_port": 8080, "size": "small"}, arch, "", [])
    assert out.files == reference_files(arch)
    assert terraform.check_files(out.files, arch)[1] == []


def test_standard_generation_still_rejects_unsafe_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TF_USE_REFERENCE", True)
    original = terraform.standard_module

    def unsafe(arch):
        out = original(arch)
        out.files[0].content += '\nresource "aws_s3_bucket" "forbidden" {}\n'
        return out

    monkeypatch.setattr(terraform, "standard_module", unsafe)
    out = terraform.gen({"project_id": "prj", "deploy_id": "dep", "recommendation": {
        "architecture": "lambda"}, "architectures": {"aws": "lambda"}}, StrandsBrain(), LocalStore(str(tmp_path)))
    assert out["status"] == "error"
    assert "aws_s3_bucket" in str(out["targets"][0]["violations"])
    assert not list(tmp_path.rglob("*.tf"))


def patch_brain(monkeypatch, patch):
    monkeypatch.setattr(config, "TF_PATCH_FIX", True)
    brain = StrandsBrain()
    monkeypatch.setattr(brain, "_tf_agent", lambda *args: None)
    calls = []

    def run(agent, prompt, schema):
        calls.append(schema)
        return patch if schema is TerraformPatch else TerraformFix(fixable=False, cause="fallback")

    monkeypatch.setattr(brain, "_run", run)
    return brain, calls


def test_patch_applies_two_typos_and_keeps_other_files(monkeypatch):
    files = reference_files("lambda")
    files[0].content = files[0].content.replace("package_type", "package_typ").replace("local.memory[", "local.memory_mb[")
    patch = TerraformPatch(fixable=True, cause="typos", edits=[
        TerraformEdit(name="main.tf", old="package_typ ", new="package_type "),
        TerraformEdit(name="main.tf", old="local.memory_mb[", new="local.memory[")])
    brain, calls = patch_brain(monkeypatch, patch)
    out = brain.fix_terraform(files, "lambda", "validate", "typos", "ref", [])
    assert out.files == reference_files("lambda")
    assert calls == [TerraformPatch]
    assert "package_typ " in files[0].content  # input remains unchanged


@pytest.mark.parametrize("name,old,arch", [("main.tf", "a", "lambda"),
    ("main.tf", "missing", "lambda"), ("new.tf", "aa", "lambda"),
    ("user_data.sh.tftpl", "aa", "ec2_compose")])
def test_ambiguous_missing_new_or_owned_file_edits_fall_back(monkeypatch, name, old, arch):
    brain, calls = patch_brain(monkeypatch, TerraformPatch(fixable=True, cause="bad edit",
        edits=[TerraformEdit(name=name, old=old, new="b")]))
    out = brain.fix_terraform([TfFile(name="main.tf", content="aa"),
        TfFile(name="user_data.sh.tftpl", content="aa")], arch, "validate", "err", "ref", [])
    assert out.cause == "fallback"
    assert calls == [TerraformPatch, TerraformFix]


def test_patch_external_failure_does_not_change_files(monkeypatch):
    brain, calls = patch_brain(monkeypatch, TerraformPatch(fixable=False, cause="quota"))
    out = brain.fix_terraform(reference_files("lambda"), "lambda", "apply", "quota", "ref", [])
    assert not out.fixable and not out.files
    assert calls == [TerraformPatch]


def test_patch_unsafe_edit_is_rejected_before_storage(tmp_path, monkeypatch):
    files = reference_files("lambda")
    brain, _ = patch_brain(monkeypatch, TerraformPatch(fixable=True, cause="unsafe",
        edits=[TerraformEdit(name="main.tf", old="memory_size", new="provisioner")]))
    out = terraform.fix({"project_id": "prj", "deploy_id": "dep", "architecture": "lambda",
        "failed_stage": "validate", "log": "err", "files": {f.name: f.content for f in files}},
        brain, LocalStore(str(tmp_path)))
    assert out["status"] == "give_up"
    assert "provisioner" in str(out["violations"])
    assert not list(tmp_path.rglob("*.tf"))


def test_repair_carries_other_cloud_and_increments_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TF_USE_REFERENCE", True)
    store = LocalStore(str(tmp_path))
    gen = terraform.gen({"project_id": "prj", "deploy_id": "dep", "recommendation":
        {"architecture": "lambda"}}, StrandsBrain(), store)
    current = gen["targets"][0]["files"]
    current["main.tf"] = current["main.tf"].replace("package_type", "package_typ")
    brain, _ = patch_brain(monkeypatch, TerraformPatch(fixable=True, cause="typo", edits=[
        TerraformEdit(name="main.tf", old="package_typ ", new="package_type ")]))
    fix = terraform.fix({"project_id": "prj", "deploy_id": "dep", "architecture": "lambda",
        "failed_stage": "validate", "log": "typo", "files": current}, brain, store)
    assert fix["status"] == "ok" and fix["saved_attempt"] == 2
    assert fix["carried_over"] == ["gcp"]
    assert store.get_text("projects/prj/deploy/dep/attempt-2/gcp/main.tf") == gen["targets"][1]["files"]["main.tf"]
