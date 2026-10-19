"""Verify claim validation with actual signed repository tokens and the real clock."""

import hashlib
import secrets
import time
from pathlib import Path

import jwt
import pytest


def test_options_preserve_claim_checks_across_verification_modes():
    repository = Path(__file__).resolve().parents[3]
    key = secrets.token_bytes(32)
    claims = {"sub": (repository / "README.md").as_uri(), "iss": repository.as_uri(),
              "document_sha256": hashlib.sha256((repository / "README.md").read_bytes()).hexdigest(),
              "exp": int(time.time()) - 60}
    expired = jwt.encode(claims, key, algorithm="HS256")
    options = {"verify_signature": False}
    before = options.copy()
    assert jwt.decode(expired, options=options) == claims
    assert options == before
    options["verify_signature"] = True
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(expired, key, algorithms=["HS256"], options=options, issuer=claims["iss"])
    current = {**claims, "exp": int(time.time()) + 120}
    signed = jwt.encode(current, key, algorithm="HS256")
    assert jwt.decode(signed, key, algorithms=["HS256"], issuer=current["iss"]) == current
