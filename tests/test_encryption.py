import pytest
from cryptography.exceptions import InvalidTag

from src.crypto.token_encryptor import TokenEncryptor, generate_encryption_key


class TestTokenEncryptor:
    @pytest.fixture
    def encryptor(self):
        return TokenEncryptor(master_key=generate_encryption_key())

    def test_encrypt_decrypt_roundtrip(self, encryptor):
        plaintext = "test_access_token_12345"
        encrypted = encryptor.encrypt(plaintext)
        assert encrypted != plaintext
        assert isinstance(encrypted, str)
        assert encryptor.decrypt(encrypted) == plaintext

    def test_encrypt_decrypt_with_aad(self, encryptor):
        plaintext = "garmin-session-blob"
        aad = "user-123"
        encrypted = encryptor.encrypt(plaintext, aad=aad)
        assert encryptor.decrypt(encrypted, aad=aad) == plaintext

    def test_wrong_aad_raises_invalid_tag(self, encryptor):
        encrypted = encryptor.encrypt("secret", aad="user-123")
        with pytest.raises(InvalidTag):
            encryptor.decrypt(encrypted, aad="user-456")

    def test_missing_aad_on_decrypt_raises_invalid_tag(self, encryptor):
        encrypted = encryptor.encrypt("secret", aad="user-123")
        with pytest.raises(InvalidTag):
            encryptor.decrypt(encrypted)

    def test_empty_string_roundtrip(self, encryptor):
        assert encryptor.decrypt(encryptor.encrypt("")) == ""

    def test_version_prefix_format(self, encryptor):
        encrypted = encryptor.encrypt("test")
        assert encrypted.startswith("v1:")
        parts = encrypted.split(":")
        assert len(parts) == 3

    def test_different_keys_incompatible(self):
        enc1 = TokenEncryptor(master_key=generate_encryption_key())
        enc2 = TokenEncryptor(master_key=generate_encryption_key())
        encrypted = enc1.encrypt("secret_token")
        with pytest.raises(InvalidTag):
            enc2.decrypt(encrypted)

    def test_invalid_key_format_raises_value_error(self):
        with pytest.raises(ValueError):
            TokenEncryptor(master_key="invalid_key_format!!!")

    def test_empty_key_raises_value_error(self):
        with pytest.raises(ValueError):
            TokenEncryptor(master_key="")

    def test_invalid_ciphertext_format_raises_value_error(self, encryptor):
        with pytest.raises(ValueError, match="Invalid ciphertext format"):
            encryptor.decrypt("not_a_valid_token")

    def test_each_encrypt_produces_unique_ciphertext(self, encryptor):
        ct1 = encryptor.encrypt("same-plaintext")
        ct2 = encryptor.encrypt("same-plaintext")
        assert ct1 != ct2

    def test_long_plaintext_roundtrip(self, encryptor):
        plaintext = "x" * 10000
        assert encryptor.decrypt(encryptor.encrypt(plaintext)) == plaintext


class TestGenerateEncryptionKey:
    def test_returns_string(self):
        key = generate_encryption_key()
        assert isinstance(key, str)
        assert len(key) > 0

    def test_key_usable_for_encryption(self):
        key = generate_encryption_key()
        enc = TokenEncryptor(master_key=key)
        assert enc.decrypt(enc.encrypt("test")) == "test"

    def test_generates_unique_keys(self):
        keys = {generate_encryption_key() for _ in range(10)}
        assert len(keys) == 10
