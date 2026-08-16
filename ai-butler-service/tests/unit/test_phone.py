from __future__ import annotations

from uuid import uuid4

import pytest

from ai_butler.adapters.sms import MockSmsProvider
from ai_butler.domain.errors import ButlerError
from ai_butler.phone import PhoneCipher, normalize_mainland_phone, phone_lookup_hash


def test_mainland_phone_normalization_and_lookup_are_deterministic() -> None:
    normalized = normalize_mainland_phone("13800138000")
    assert normalized == "+8613800138000"
    assert normalize_mainland_phone(normalized) == normalized
    assert phone_lookup_hash(normalized, "lookup-secret") == phone_lookup_hash(
        normalized, "lookup-secret"
    )


def test_invalid_phone_is_rejected_without_echoing_input() -> None:
    with pytest.raises(ButlerError) as error:
        normalize_mainland_phone("123456")
    assert error.value.code == "INVALID_PHONE_NUMBER"
    assert "123456" not in error.value.message


def test_phone_cipher_uses_random_nonce_and_authenticates_ciphertext() -> None:
    cipher = PhoneCipher("synthetic-phone-encryption-secret-32-characters")
    first = cipher.encrypt("+8613800138000")
    second = cipher.encrypt("+8613800138000")
    assert first != second
    assert cipher.decrypt(first) == "+8613800138000"
    with pytest.raises(ValueError, match="invalid phone ciphertext"):
        cipher.decrypt(f"{first[:-1]}A")


async def test_mock_sms_retains_only_challenge_identifier() -> None:
    provider = MockSmsProvider()
    challenge_id = uuid4()
    message_id = await provider.send_login_code("+8613800138000", "123456", challenge_id)
    assert provider.sent_challenges == {challenge_id}
    assert message_id == f"mock-{challenge_id}"
    assert "13800138000" not in repr(provider)
    assert "123456" not in repr(provider)
