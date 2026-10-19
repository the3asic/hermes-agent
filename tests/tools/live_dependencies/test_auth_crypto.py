"""Exercise real PKCE challenges and signatures over existing repository documents."""

import base64
import hashlib
import secrets
from pathlib import Path

import pytest
from nacl.exceptions import BadSignatureError
from nacl.secret import SecretBox
from nacl.signing import SigningKey
from oauthlib.oauth2.rfc6749.grant_types.authorization_code import (
    code_challenge_method_plain,
    code_challenge_method_s256,
)


def test_auth_crypto_preserves_repository_document():
    repository = Path(__file__).resolve().parents[3]
    document = (repository / "README.md").read_bytes()
    other_document = (repository / "AGENTS.md").read_bytes()
    key = SigningKey.generate()
    signed = key.sign(document)
    assert key.verify_key.verify(signed) == document
    with pytest.raises(BadSignatureError):
        key.verify_key.verify(signed.signature + other_document)
    box = SecretBox(secrets.token_bytes(SecretBox.KEY_SIZE))
    assert box.decrypt(box.encrypt(document)) == document
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert code_challenge_method_s256(verifier, challenge)
    assert code_challenge_method_plain(verifier, verifier)
    assert not code_challenge_method_s256(secrets.token_urlsafe(48), challenge)
