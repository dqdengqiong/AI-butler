"""手机号规范化、不可逆查询标识与可轮换密文。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_butler.domain.errors import ButlerError

MAINLAND_PHONE_PATTERN = re.compile(r"^(?:\+86)?(1[3-9]\d{9})$")
PHONE_CIPHERTEXT_VERSION = "v1"


def normalize_mainland_phone(value: str) -> str:
    """校验中国大陆手机号并统一返回 ``+86`` E.164 形式。"""

    match = MAINLAND_PHONE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ButlerError("INVALID_PHONE_NUMBER", "请输入有效的中国大陆手机号", 422)
    return f"+86{match.group(1)}"


def phone_lookup_hash(phone: str, secret: str) -> str:
    """生成仅供等值查询使用的手机号 HMAC，避免低熵手机号被离线枚举。"""

    return hmac.new(secret.encode(), phone.encode(), hashlib.sha256).hexdigest()


class PhoneCipher:
    """使用带版本前缀的 AES-256-GCM 加密手机号。

    配置值经 SHA-256 派生为固定长度 AES 密钥，使部署配置无需承担二进制
    编码差异；每次写入都使用独立 nonce，因此相同手机号不会产生相同密文。
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("phone encryption secret must contain at least 32 characters")
        self._cipher = AESGCM(hashlib.sha256(secret.encode()).digest())

    def encrypt(self, phone: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, phone.encode(), PHONE_CIPHERTEXT_VERSION.encode())
        return ".".join(
            (
                PHONE_CIPHERTEXT_VERSION,
                _encode(nonce),
                _encode(ciphertext),
            )
        )

    def decrypt(self, value: str) -> str:
        """解密受保护手机号；版本、认证标签或结构错误均显式失败。"""

        try:
            version, encoded_nonce, encoded_ciphertext = value.split(".", 2)
            if version != PHONE_CIPHERTEXT_VERSION:
                raise ValueError("unsupported phone ciphertext version")
            plaintext = self._cipher.decrypt(
                _decode(encoded_nonce),
                _decode(encoded_ciphertext),
                version.encode(),
            )
            return plaintext.decode()
        except (ValueError, UnicodeDecodeError, binascii.Error, InvalidTag) as exc:
            raise ValueError("invalid phone ciphertext") from exc


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")
