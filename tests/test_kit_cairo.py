"""The cairo kit, against real snforge 0.59 output.

The fixtures are unedited logs (temp paths aside) from the falcon1024 and
mldsa87 season baselines, and a deliberately failing test. The numbers they
assert are the ones those domains' ledgers already hold, so the kit is held to
reproducing what the hand-written parsers measured.
"""
import json
import pathlib
import subprocess

import pytest

from autoresearch.kits import cairo
from autoresearch.kits.cairo import KitError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "snforge"


def log(name):
    return (FIXTURES / f"{name}.log").read_text()


# -- parsing: the numbers the domains already recorded ----------------------

def test_falcon_baseline_reproduces_its_documented_gas():
    """falcon1024 scores Sierra gas: gas17 = verify - baseline = 144,846,064."""
    tests = cairo.parse_snforge(log("falcon-bench"))
    assert len(tests) == 17 and not cairo.failures(tests)
    verify = cairo.gas(tests, "bench_verify_falcon_1024", "sierra")
    base = cairo.gas(tests, "bench_baseline_falcon_1024", "sierra")
    assert verify - base == 144_846_064


def test_mldsa_baseline_reproduces_its_ledger_row():
    """mldsa87 scores L2 gas: validate - baseline = 401,268,185, ledger row
    6061af29acef; setup gas 2,383,918,128 on the same row."""
    tests = cairo.parse_snforge(log("mldsa-accounts"))
    assert (cairo.gas(tests, "bench_validate_ml_dsa_87")
            - cairo.gas(tests, "bench_baseline_ml_dsa_87")) == 401_268_185
    assert cairo.gas(tests, "bench_setup_ml_dsa_87") == 2_383_918_128


def test_l2_and_sierra_gas_are_read_separately():
    """They agree only where a test makes no syscalls; an account's validate
    pays for its syscalls in L2 gas, so a domain must say which it is scored in."""
    tests = cairo.parse_snforge(log("mldsa-accounts"))
    assert cairo.gas(tests, "bench_validate_ml_dsa_87", "l2") == 2_785_489_633
    assert cairo.gas(tests, "bench_validate_ml_dsa_87", "sierra") == 2_077_165_633
    falcon = cairo.parse_snforge(log("falcon-bench"))
    assert all(t.l2_gas == t.sierra_gas for t in falcon.values())   # no syscalls

    without = cairo.parse_snforge(log("failing").replace("sierra gas:", "sierra:"))
    with pytest.raises(KitError, match="no sierra gas"):
        cairo.gas(without, "zz_passes", "sierra")


def test_a_failing_test_is_reported_with_its_failure_text():
    tests = cairo.parse_snforge(log("failing"))
    assert cairo.failures(tests) == ["zz_asserts"]
    assert "MISMATCH" in tests["zz_asserts"].failure
    assert tests["zz_passes"].l2_gas == 13_840
    with pytest.raises(KitError, match="zz_asserts failed"):
        cairo.gas(tests, "zz_asserts")


# -- parsing: the silent failures it exists to make loud ---------------------

def test_a_result_the_parser_missed_is_refused_not_dropped():
    """Lose one `[PASS]` line -- what a format change does to a regex -- and the
    count no longer matches snforge's own summary."""
    text = log("falcon-bench").replace(
        "[PASS] pqbench_falcon_1024_integrationtest::vectors::key_short",
        "[OK] pqbench_falcon_1024_integrationtest::vectors::key_short")
    with pytest.raises(KitError, match="17 passed / 0 failed but 16 / 0"):
        cairo.parse_snforge(text)


def test_a_gas_field_that_moved_is_an_error_not_a_none():
    text = log("mldsa-accounts").replace("l2_gas: ~", "l2: ~")
    tests = cairo.parse_snforge(text)
    with pytest.raises(KitError, match="reported no l2 gas"):
        cairo.gas(tests, "bench_validate_ml_dsa_87")


def test_a_build_that_never_ran_tests_is_refused():
    with pytest.raises(KitError, match="no `Tests:` summary"):
        cairo.parse_snforge("error: could not compile `pq_accounts`\n")


def test_two_tests_with_one_name_are_refused():
    text = log("falcon-bench").replace("vectors::key_short", "bench::bench_verify_falcon_1024")
    with pytest.raises(KitError, match="two tests named 'bench_verify_falcon_1024'"):
        cairo.parse_snforge(text)


def test_a_test_that_did_not_run_is_named():
    tests = cairo.parse_snforge(log("failing"))
    with pytest.raises(KitError, match="bench_verify_x did not run"):
        cairo.gas(tests, "bench_verify_x")


# -- the submit rules --------------------------------------------------------

SURFACE = ["crates/falcon_1024/src/*.cairo", "crates/ntt/src/*.cairo"]
FORBIDDEN = ["*/fixtures*"]


def diff(path, body="+    let x = 1;\n", old=None, new=None):
    return (f"diff --git a/{path} b/{path}\n"
            f"--- {old or 'a/' + path}\n+++ {new or 'b/' + path}\n"
            f"@@ -1,0 +1,1 @@\n{body}")


@pytest.mark.parametrize("patch, reason", [
    (diff("crates/falcon_1024/src/ntt.cairo"), None),
    (diff("crates/ntt/src/deep/mod.cairo"), None),
    (diff("crates/falcon_1024/src/new.cairo", old="/dev/null"), None),          # added
    (diff("crates/falcon_1024/src/a.cairo", "+#[cfg(test)]\n"), None),
    (diff("crates/falcon_1024/tests/bench.cairo"), "outside the edit surface"),
    (diff("crates/falcon_1024/src/fixtures.cairo"), "outside the edit surface"),
    (diff("crates/falcon_1024/src/lib.rs"), "outside the edit surface"),
    (diff("crates/falcon_1024/src/a.cairo", new="/dev/null"), "deletes or renames"),
    (diff("crates/falcon_1024/src/../../../etc.cairo"), "escapes the repository"),
    (diff("crates/falcon_1024/src/a.cairo", "+#[cfg(feature: 'x')]\n"), "cfg other than"),
    ("rename from crates/falcon_1024/src/a.cairo\n", "deletes or renames"),
])
def test_check_patch(patch, reason):
    got = cairo.check_patch(patch, SURFACE, FORBIDDEN)
    assert (got is None) if reason is None else (reason in got), got


def test_check_patch_refuses_an_oversized_patch():
    patch = diff("crates/ntt/src/a.cairo", "+" + "x" * 300_000 + "\n")
    assert "bytes >" in cairo.check_patch(patch, SURFACE)


# -- applying, inside the repository that bit SLH-DSA-128 H3 -----------------

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    *args], cwd=cwd, check=True, capture_output=True)


def test_a_patch_lands_in_a_tree_nested_inside_another_repository(tmp_path):
    """git apply in a directory under an enclosing repo resolves paths from
    that repo's root and skips them all, exit 0. The kit fences discovery off
    and proves the result."""
    _git(tmp_path, "init", "-q")
    tree = tmp_path / "data" / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.cairo").write_text("fn a() {}\n")
    patch = ("--- a/src/a.cairo\n+++ b/src/a.cairo\n"
             "@@ -1 +1 @@\n-fn a() {}\n+fn a() { 1 }\n")
    assert cairo.apply_patch(tree, patch) is None
    assert (tree / "src" / "a.cairo").read_text() == "fn a() { 1 }\n"


def test_a_patch_that_does_not_apply_says_so(tmp_path):
    (tmp_path / "a.cairo").write_text("fn b() {}\n")
    patch = "--- a/a.cairo\n+++ b/a.cairo\n@@ -1 +1 @@\n-fn a() {}\n+fn a() { 1 }\n"
    assert cairo.apply_patch(tmp_path, patch).startswith("git apply:")


def test_export_takes_the_commit_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.cairo").write_text("committed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.cairo").write_text("edited, never committed\n")
    (repo / "stray.cairo").write_text("untracked\n")
    out = cairo.export(repo, "HEAD", tmp_path / "out")
    assert (out / "a.cairo").read_text() == "committed\n"
    assert not (out / "stray.cairo").exists()


# -- the declare caps ---------------------------------------------------------

def _account(tree, class_bytes, casm_felts):
    target = tree / "target" / "dev"
    target.mkdir(parents=True)
    (target / "pq_accounts_A.contract_class.json").write_bytes(b"x" * class_bytes)
    (target / "pq_accounts_A.compiled_contract_class.json").write_text(
        json.dumps({"bytecode": ["0x0"] * casm_felts}))


def test_an_account_at_the_caps_is_declarable(tmp_path):
    _account(tmp_path, cairo.MAX_CLASS_BYTES, cairo.MAX_CASM_FELTS)
    sizes = cairo.declare_sizes(tmp_path, "pq_accounts", "A")
    assert sizes == {"class_bytes": 4_089_446, "casm_felts": 81_920}
    assert cairo.over_caps(sizes) is None


def test_one_byte_over_either_cap_is_not_a_result():
    """falcon1024 recorded cad820c90290 as ok at 4,090,517 class bytes."""
    assert "class_bytes 4,090,517 > 4,089,446" in cairo.over_caps(
        {"class_bytes": 4_090_517, "casm_felts": 77_000})
    assert "casm_felts 81,921" in cairo.over_caps(
        {"class_bytes": 1_000, "casm_felts": 81_921})


def test_a_missing_class_names_the_build_setting(tmp_path):
    with pytest.raises(KitError, match="casm = true"):
        cairo.declare_sizes(tmp_path, "pq_accounts", "A")


def test_the_account_is_built_with_audited_libfuncs(tmp_path):
    toml = tmp_path / "pq-accounts" / "contracts" / "Scarb.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text('[[target.starknet-contract]]\nallowed-libfuncs-list.name = "experimental"\n')
    cairo.audited_libfuncs(tmp_path)
    assert 'allowed-libfuncs-list.name = "audited"' in toml.read_text()


def test_a_libfunc_switch_with_nothing_to_switch_is_refused(tmp_path):
    toml = tmp_path / "pq-accounts" / "contracts" / "Scarb.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("[[target.starknet-contract]]\n")
    with pytest.raises(KitError, match="cannot require audited"):
        cairo.audited_libfuncs(tmp_path)


# -- the sandbox --------------------------------------------------------------

def test_the_sandbox_has_no_network_unless_asked(tmp_path):
    box = cairo.Sandbox(image="img:1", cache_volume="cache")
    argv = box.argv(tmp_path, ["snforge", "test"])
    assert argv[argv.index("--network") + 1] == "none"
    assert "SCARB_OFFLINE=true" in argv and argv[-3:] == ["img:1", "snforge", "test"]
    assert "--network" not in box.argv(tmp_path, ["scarb", "fetch"], network=True)
