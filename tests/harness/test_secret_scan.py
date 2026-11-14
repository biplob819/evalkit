import pytest

import secret_scan

FAKES = {
    "anthropic key": "sk-ant-" + "a1B2" * 10,
    "openai key": "sk-proj-" + "Z9y8" * 10,
    "aws access key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github token": "ghp_" + "x" * 36,
    "pypi token": "pypi-" + "A" * 60,
    "private key": "-----BEGIN RSA " + "PRIVATE KEY-----",
}
MORE = {
    "github fine-grained token": "github_pat_" + "A1b2" * 15,
    "aws access key (session)": "ASIA" + "ABCDEFGHIJKLMNOP",
    "aws secret key": "aws_secret_access_key = " + "a/B+" * 10,
    "encrypted private key": "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----",
    "dsa private key": "-----BEGIN DSA " + "PRIVATE KEY-----",
    "pgp private key": "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----",
}


def test_self_test_passes():
    assert secret_scan.self_test() == 0


@pytest.mark.parametrize("kind", sorted(FAKES))
def test_detects_each_kind(tmp_path, kind):
    f = tmp_path / "leak.txt"
    f.write_text(f"value = {FAKES[kind]}\n", encoding="utf-8")
    hits = secret_scan.scan([f])
    assert hits and kind in hits[0]


@pytest.mark.parametrize("label", sorted(MORE))
def test_detects_formats_found_by_the_verifier(tmp_path, label):
    f = tmp_path / ".env.example"
    f.write_text(f"{MORE[label]}\n", encoding="utf-8")
    assert secret_scan.scan([f]), label


def test_non_utf8_file_is_still_scanned(tmp_path):
    f = tmp_path / "latin1.txt"
    f.write_bytes(b"caf\xe9 " + FAKES["anthropic key"].encode() + b"\n")
    assert secret_scan.scan([f])


def test_scan_is_deterministic(tmp_path):
    for i, kind in enumerate(sorted(FAKES)):
        (tmp_path / f"f{i}.txt").write_text(FAKES[kind] + "\n", encoding="utf-8")
    files = sorted(tmp_path.iterdir())
    assert secret_scan.scan(files) == secret_scan.scan(files)
    assert len(secret_scan.scan(files)) == len(FAKES)


def test_env_var_reference_is_clean(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text('key = os.environ["OPENAI_API_KEY"]\n', encoding="utf-8")
    assert secret_scan.scan([f]) == []


@pytest.mark.safety
def test_live_repo_has_no_secrets():
    assert secret_scan.scan(secret_scan.candidate_files(secret_scan.ROOT)) == []
