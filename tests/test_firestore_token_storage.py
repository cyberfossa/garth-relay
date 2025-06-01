import json
from typing import cast
from unittest.mock import MagicMock

import pytest
from cryptography.exceptions import InvalidTag
from garth.auth_tokens import OAuth2Token
from garth.utils import asdict
from google.api_core.exceptions import ServiceUnavailable

from src.db.firestore_token_storage import ENCRYPTED_FIELDS, FirestoreTokenStorage


def _make_token() -> OAuth2Token:
    return OAuth2Token(
        scope="CONNECT_READ CONNECT_WRITE",
        jti="test-jti-123",
        token_type="Bearer",
        access_token="access-token-abc",
        refresh_token="refresh-token-xyz",
        expires_in=3600,
        expires_at=1700000000,
        refresh_token_expires_in=7776000,
        refresh_token_expires_at=1707776000,
        mfa_token="mfa-token-789",
        mfa_expiration_timestamp="2024-01-01T01:00:00Z",
        mfa_expiration_timestamp_millis=1704067200000,
        client_id="client-123",
    )


def _stored_token_data(token: OAuth2Token, encryptor, user_id: str) -> dict[str, object]:
    token_data: dict[str, object] = {}
    for key, value in cast(dict[str, object], asdict(token)).items():
        serialized = json.dumps(value)
        token_data[key] = encryptor.encrypt(serialized, aad=user_id) if key in ENCRYPTED_FIELDS else serialized
    return token_data


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_encryptor():
    encryptor = MagicMock()
    encryptor.encrypt = MagicMock(side_effect=lambda data, aad: f"v1:{data}")
    encryptor.decrypt = MagicMock(side_effect=lambda data, aad: data.removeprefix("v1:"))
    return encryptor


@pytest.fixture
def storage(mock_db, mock_encryptor):
    return FirestoreTokenStorage(user_id="user-123", db=mock_db, encryptor=mock_encryptor)


@pytest.fixture
def doc_ref(mock_db):
    ref = MagicMock()
    mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value = ref
    return ref


class TestLoad:
    def test_returns_none_when_document_missing(self, storage, doc_ref):
        doc_ref.get.return_value.exists = False
        assert storage.load() is None

    def test_propagates_infra_errors(self, storage, doc_ref):
        doc_ref.get.side_effect = ServiceUnavailable("Firestore down")
        with pytest.raises(ServiceUnavailable):
            storage.load()

    def test_returns_none_on_decryption_failure(self, storage, doc_ref):
        token = _make_token()
        stored = _stored_token_data(token, storage._encryptor, "user-123")
        doc_ref.get.return_value.exists = True
        doc_ref.get.return_value.to_dict.return_value = stored

        storage._encryptor.decrypt = MagicMock(side_effect=InvalidTag())
        assert storage.load() is None

    def test_returns_token_when_valid(self, storage, doc_ref):
        token = _make_token()
        stored = _stored_token_data(token, storage._encryptor, "user-123")
        stored["updated_at"] = "2024-01-01T00:00:00Z"

        doc_ref.get.return_value.exists = True
        doc_ref.get.return_value.to_dict.return_value = stored

        result = storage.load()
        assert isinstance(result, OAuth2Token)
        assert result == token

    def test_returns_none_on_missing_key(self, storage, doc_ref):
        doc_ref.get.return_value.exists = True
        doc_ref.get.return_value.to_dict.return_value = {"access_token": 'v1:"tok"'}
        assert storage.load() is None


class TestSave:
    def test_stores_non_sensitive_fields_as_plaintext_json(self, storage, doc_ref):
        token = _make_token()
        storage.save(token)

        saved_data = doc_ref.set.call_args[0][0]
        assert saved_data["expires_in"] == json.dumps(token.expires_in)
        assert saved_data["token_type"] == json.dumps(token.token_type)

    def test_stores_sensitive_fields_encrypted(self, storage, doc_ref):
        token = _make_token()
        storage.save(token)

        saved_data = doc_ref.set.call_args[0][0]
        assert saved_data["access_token"] == f"v1:{json.dumps(token.access_token)}"
        assert saved_data["refresh_token"] == f"v1:{json.dumps(token.refresh_token)}"
        assert saved_data["mfa_token"] == f"v1:{json.dumps(token.mfa_token)}"
        assert doc_ref.set.call_args[1] == {"merge": True}

    def test_reraises_on_firestore_write_failure(self, storage, doc_ref):
        doc_ref.set.side_effect = Exception("Firestore unavailable")
        token = _make_token()
        with pytest.raises(Exception, match="Firestore unavailable"):
            storage.save(token)

    def test_round_trip(self, mock_db, mock_encryptor):
        storage = FirestoreTokenStorage(user_id="user-123", db=mock_db, encryptor=mock_encryptor)
        ref = MagicMock()
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value = ref

        token = _make_token()
        storage.save(token)

        saved_data = ref.set.call_args[0][0]
        ref.get.return_value.exists = True
        ref.get.return_value.to_dict.return_value = saved_data

        loaded = storage.load()
        assert loaded is not None
        assert loaded == token


class TestDelete:
    def test_delete_calls_firestore(self, storage, doc_ref):
        storage.delete()
        doc_ref.delete.assert_called_once()
