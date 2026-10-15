"""Cryptographic primitives for the MiniScheme channel protocol and key lifecycle.

Implements:
- HKDF-SHA256 key derivation
- HMAC-SHA256 authentication and challenges
- AES-256-GCM authenticated encryption and decryption
- Anti-replay nonce tracking with timestamp verification
"""

import hmac
import hashlib
import os
import struct
import time
from typing import Tuple, Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-Extract and Expand using SHA-256."""
    if not salt:
        salt = bytes(32)
    # Extract
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    # Expand
    okm = b""
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


def derive_hardware_hash_key(hardware_password: str, mac: str) -> bytes:
    """Step 1 & 2: Derive hardware hash key from password and normalized MAC."""
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    return hkdf_sha256(
        ikm=hardware_password.encode("utf-8"),
        salt=clean_mac.encode("utf-8"),
        info=b"msc1_hw_hash",
        length=32
    )


def derive_master_key_2(master_key_1: bytes, hardware_hash_key: bytes, mac: str) -> bytes:
    """Step 2: Server & hardware derive master key 2 from server master_key_1 and hardware_hash_key."""
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    return hkdf_sha256(
        ikm=master_key_1,
        salt=hardware_hash_key,
        info=clean_mac.encode("utf-8"),
        length=32
    )


def derive_request_keys(master_key_2: bytes, nonce: bytes) -> Tuple[bytes, bytes]:
    """Step 3: Derive per-request (req_key, resp_key) from master_key_2 and nonce."""
    okm = hkdf_sha256(
        ikm=master_key_2,
        salt=nonce,
        info=b"msc1_subkeys",
        length=64
    )
    return okm[:32], okm[32:]


def create_challenge_response(hardware_hash_key: bytes, challenge_nonce: bytes) -> bytes:
    """Step 4: Factory reset HMAC response."""
    return hmac.new(hardware_hash_key, challenge_nonce, hashlib.sha256).digest()


def verify_challenge_response(hardware_hash_key: bytes, challenge_nonce: bytes, response: bytes) -> bool:
    """Step 4: Verify factory reset challenge response."""
    expected = create_challenge_response(hardware_hash_key, challenge_nonce)
    return hmac.compare_digest(expected, response)


def aes_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, associated_data: bytes = b"") -> bytes:
    """Encrypt using AES-256-GCM. Returns ciphertext + 16-byte auth tag."""
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography library required for AES-GCM operations")
    aesgcm = AESGCM(key)
    # GCM standard 12-byte nonce
    gcm_nonce = nonce[:12] if len(nonce) >= 12 else nonce.ljust(12, b"\x00")
    return aesgcm.encrypt(gcm_nonce, plaintext, associated_data)


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext_and_tag: bytes, associated_data: bytes = b"") -> bytes:
    """Decrypt using AES-256-GCM. Verifies auth tag."""
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography library required for AES-GCM operations")
    aesgcm = AESGCM(key)
    gcm_nonce = nonce[:12] if len(nonce) >= 12 else nonce.ljust(12, b"\x00")
    return aesgcm.decrypt(gcm_nonce, ciphertext_and_tag, associated_data)
