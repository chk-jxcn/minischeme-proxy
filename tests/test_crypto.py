import pytest
from proxy.crypto import (
    hkdf_sha256,
    derive_hardware_hash_key,
    derive_master_key_2,
    derive_request_keys,
    create_challenge_response,
    verify_challenge_response,
    aes_gcm_encrypt,
    aes_gcm_decrypt,
    HAS_CRYPTOGRAPHY
)


def test_hkdf_basic():
    ikm = b"secret_input_material"
    salt = b"salt_bytes_12345"
    info = b"info_string"
    derived = hkdf_sha256(ikm, salt, info, 32)
    assert len(derived) == 32
    assert derived == hkdf_sha256(ikm, salt, info, 32)


def test_device_key_lifecycle():
    hw_pwd = "my_strong_hardware_password"
    mac = "C8:F0:9E:B3:6C:90"
    m1 = b"server_level_1_master_key_32bytes"

    # Step 1: derive hardware hash key
    hw_hash = derive_hardware_hash_key(hw_pwd, mac)
    assert len(hw_hash) == 32

    # Step 2: derive master key 2
    m2 = derive_master_key_2(m1, hw_hash, mac)
    assert len(m2) == 32

    # Step 3: per-request subkeys
    nonce = b"1234567890123456"
    k_req, k_resp = derive_request_keys(m2, nonce)
    assert len(k_req) == 32
    assert len(k_resp) == 32
    assert k_req != k_resp

    # Step 4: Factory reset challenge-response
    ch_nonce = b"random_server_challenge_nonce_32"
    resp = create_challenge_response(hw_hash, ch_nonce)
    assert verify_challenge_response(hw_hash, ch_nonce, resp) is True
    assert verify_challenge_response(hw_hash, ch_nonce, b"wrong_response") is False


@pytest.mark.skipif(not HAS_CRYPTOGRAPHY, reason="cryptography library not installed")
def test_aes_gcm():
    key = b"32_byte_aes_key_for_testing_1234"
    nonce = b"16_byte_nonce_12"
    plaintext = b"Hello, MiniScheme Encrypted Channel!"
    aad = b"associated_authenticated_data"

    ciphertext = aes_gcm_encrypt(key, nonce, plaintext, aad)
    decrypted = aes_gcm_decrypt(key, nonce, ciphertext, aad)
    assert decrypted == plaintext
