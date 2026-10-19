"""Run real maintained-release dependency checks with verifiable source receipts."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from xml.etree import ElementTree


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bash", required=True)
    parser.add_argument("--uv", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    receipt = out / "acceptance.json"
    assert not receipt.exists(), "Read the existing acceptance before repeating it"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert head == args.expected_commit
    assert os.path.samefile(os.environ["HERMES_PYTHON"], sys.executable)
    targets = {"aiohttp": "3.14.4", "httpcore2": "2.12.0", "httpx2": "2.12.0", "mcp": "2.2.0",
               "multidict": "6.9.1", "oauthlib": "4.0.0", "pip": "26.2", "pyjwt": "2.15.0",
               "pynacl": "1.6.2", "setuptools": "83.0.0", "tornado": "6.5.9", "urllib3": "2.8.0"}
    actual = {name: metadata.version(name) for name in targets}
    assert actual == targets, actual
    files = {"test_mcp_http_transport.py": 2, "test_jwt_options.py": 1,
             "test_network_clients.py": 1, "test_auth_crypto.py": 1, "test_cache_diagnostic.py": 2}
    results = []
    for name, expected in files.items():
        path = "tests/tools/live_dependencies/" + name
        junit = out / (name + ".xml")
        command = [args.bash, "scripts/run_tests.sh", path, "--file-timeout", "300", "--file-retries", "0",
                   "--", "--noconftest", "--basetemp=" + str(out / (name + "-cases")), "--junitxml=" + str(junit), "-q"]
        with (out / (name + ".stdout")).open("wb") as stdout, (out / (name + ".stderr")).open("wb") as stderr:
            process = subprocess.run(command, cwd=root, stdout=stdout, stderr=stderr, timeout=360)
        xml = ElementTree.parse(junit)
        counts = {key: sum(int(suite.get(key, "0")) for suite in xml.iter("testsuite"))
                  for key in ("tests", "failures", "errors", "skipped")}
        result = {"test": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                  "exit_code": process.returncode, "counts": counts}
        results.append(result)
        (out / "test-progress.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        assert process.returncode == 0 and counts == {"tests": expected, "failures": 0, "errors": 0, "skipped": 0}, result
    checks = {
        "ruff": [sys.executable, "-m", "ruff", "check", "."],
        "windows": [sys.executable, "scripts/check-windows-footguns.py", "--all"],
        "compat": [sys.executable, "scripts/check_compat_pointers.py"],
        "temp_paths": [sys.executable, "scripts/check_no_tmp_literals.py"],
        "yaml_writers": [sys.executable, "scripts/check_config_yaml_writers.py"],
        "os_markers": [sys.executable, "scripts/ci/check_os_marker_fakes.py"],
        "profile_scope": [sys.executable, "scripts/check_profile_scope_patterns.py", "--base", "ed063b312be2f1f50c470c70c8b96900ea4f3fc3", "--head", head],
        "public_surface": [sys.executable, "scripts/ci/check_public_surface.py", "--base", "ed063b312be2f1f50c470c70c8b96900ea4f3fc3", "--head", head],
        "lock": [args.uv, "lock", "--check", "--python", sys.executable],
    }
    for name, command in checks.items():
        with (out / (name + ".stdout")).open("wb") as stdout, (out / (name + ".stderr")).open("wb") as stderr:
            subprocess.run(command, cwd=root, stdout=stdout, stderr=stderr, timeout=300, check=True)
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    assert status == []
    record = {"commit": head, "tree": tree, "python": sys.executable, "python_version": sys.version,
              "versions": actual, "tests": results, "checks": list(checks), "tracked_status": status,
              "actual_tests_passed": sum(row["counts"]["tests"] for row in results), "accepted": True}
    receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"commit": head, "accepted": True, "actual_tests_passed": record["actual_tests_passed"], "checks_passed": len(checks)}))


if __name__ == "__main__":
    main()
