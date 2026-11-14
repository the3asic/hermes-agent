"""Verify maintained dependencies and real transports on an exact source revision."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from xml.etree import ElementTree

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_RUNTIME_PACKAGES = (
    "aiohttp", "anyio", "httpcore2", "httpx2", "mcp", "multidict",
    "oauthlib", "pyjwt", "pynacl", "urllib3",
)
_TRANSPORT_TESTS = (
    "test_mcp_http_transport.py", "test_jwt_options.py", "test_network_clients.py",
    "test_auth_crypto.py", "test_cache_diagnostic.py",
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    ).stdout.strip()


def _locked_runtime_versions(root: Path) -> dict[str, str]:
    """Require installed versions to belong to this source's lock and direct constraints."""
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions: dict[str, set[str]] = {}
    for package in lock["package"]:
        versions.setdefault(canonicalize_name(package["name"]), set()).add(package["version"])
    actual = {name: metadata.version(name) for name in _RUNTIME_PACKAGES}
    for name, version in actual.items():
        if version not in versions.get(canonicalize_name(name), set()):
            raise RuntimeError(f"Installed {name} {version} does not match the committed lock")
    requirements = list(project["dependencies"])
    requirements.extend(
        requirement for group in project.get("optional-dependencies", {}).values() for requirement in group
    )
    for text in requirements:
        requirement = Requirement(text)
        name = canonicalize_name(requirement.name)
        if name not in actual or (requirement.marker and not requirement.marker.evaluate()):
            continue
        if requirement.specifier and not requirement.specifier.contains(actual[name]):
            raise RuntimeError(f"Installed {name} {actual[name]} violates {requirement}")
    return actual


def _transport_check(root: Path, out: Path, bash: str, name: str) -> dict:
    path = "tests/tools/live_dependencies/" + name
    junit = out / (name + ".xml")
    command = [
        bash, "scripts/run_tests.sh", path, "--file-timeout", "300", "--file-retries", "0",
        "--", "--noconftest", "--basetemp=" + str(out / (name + "-cases")),
        "--junitxml=" + str(junit), "-q",
    ]
    with (out / (name + ".stdout")).open("wb") as stdout, (out / (name + ".stderr")).open("wb") as stderr:
        process = subprocess.run(
            command, cwd=root, stdout=stdout, stderr=stderr, timeout=360, check=False,
        )
    xml = ElementTree.parse(junit)
    counts = {
        key: sum(int(suite.get(key, "0")) for suite in xml.iter("testsuite"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    result = {
        "test": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
        "exit_code": process.returncode, "counts": counts,
    }
    if process.returncode or counts["tests"] <= 0 or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise RuntimeError(f"Real dependency verification failed: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    receipt = out / "acceptance.json"
    if receipt.exists():
        raise RuntimeError("Read the existing acceptance before repeating it")
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if head != args.expected_commit:
        raise RuntimeError("The checkout does not match the expected commit")
    if not os.path.samefile(os.environ["HERMES_PYTHON"], sys.executable):
        raise RuntimeError("Acceptance and the canonical runner must use the same interpreter")
    if _git(root, "status", "--porcelain"):
        raise RuntimeError("Acceptance requires a clean source revision")
    from pm.install import env_for
    os.environ.update(env_for("python", "node", "npm", "pnpm", "uv", "ripgrep"))
    actual = _locked_runtime_versions(root)
    results = []
    for name in _TRANSPORT_TESTS:
        results.append(_transport_check(root, out, args.bash, name))
        (out / "test-progress.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    checks = {
        "repository": [sys.executable, "scripts/check", "--base", args.base],
        "lock": [sys.executable, "-m", "pm.build_env", "--source", str(root), "--check-lock"],
    }
    for name, command in checks.items():
        with (out / (name + ".stdout")).open("wb") as stdout, (out / (name + ".stderr")).open("wb") as stderr:
            subprocess.run(command, cwd=root, stdout=stdout, stderr=stderr, timeout=3900, check=True)
    status = _git(root, "status", "--porcelain").splitlines()
    if status:
        raise RuntimeError(f"Verification changed tracked source: {status}")
    record = {
        "commit": head, "tree": tree, "python": sys.executable, "python_version": sys.version,
        "versions": actual, "tests": results, "checks": list(checks), "tracked_status": status,
        "actual_tests_passed": sum(row["counts"]["tests"] for row in results), "accepted": True,
    }
    receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"commit": head, "accepted": True, "actual_tests_passed": record["actual_tests_passed"]}))


if __name__ == "__main__":
    main()
