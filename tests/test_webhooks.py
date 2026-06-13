"""Tests for webhook routes."""

import base64
import io
import json

import pytest
import tink
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tink import cleartext_keyset_handle, signature

from src.routes.webhooks import create_webhooks_router


@pytest.fixture(scope="module", autouse=True)
def register_tink():
    signature.register()


@pytest.fixture
def tink_keys():
    """Generate a test ECDSA P-256 keyset for signing and verification."""
    # Generate private keyset handle
    template = signature.signature_key_templates.ECDSA_P256
    private_handle = tink.new_keyset_handle(template)

    # Get public keyset handle
    public_handle = private_handle.public_keyset_handle()

    # Serialize private keyset
    private_stream = io.StringIO()
    private_keyset_writer = tink.JsonKeysetWriter(private_stream)
    cleartext_keyset_handle.write(private_keyset_writer, private_handle)
    private_keyset_json = private_stream.getvalue()

    # Serialize public keyset
    public_stream = io.StringIO()
    public_keyset_writer = tink.JsonKeysetWriter(public_stream)
    cleartext_keyset_handle.write(public_keyset_writer, public_handle)
    public_keyset_json = public_stream.getvalue()

    return {
        "private_handle": private_handle,
        "public_handle": public_handle,
        "private_json": private_keyset_json,
        "public_json": public_keyset_json,
    }


@pytest.fixture
def mock_db(mocker):
    db = mocker.MagicMock()
    return db


@pytest.fixture
def mock_sync_orchestrator(mocker):
    sync = mocker.MagicMock()
    return sync


@pytest.fixture
def webhook_secret():
    return "test-webhook-secret-token"


@pytest.fixture
def client(mock_db, mock_sync_orchestrator, webhook_secret):
    app = FastAPI()
    router = create_webhooks_router(
        db_client=mock_db,
        sync_orchestrator=mock_sync_orchestrator,
        webhook_secret=webhook_secret,
    )
    app.include_router(router)
    return TestClient(app)


class TestGoogleHealthWebhook:
    def test_handshake_authorized_with_bearer(self, client, webhook_secret):
        payload = {"type": "verification"}
        response = client.post(
            "/webhooks/google-health",
            json=payload,
            headers={"Authorization": f"Bearer {webhook_secret}"},
        )
        assert response.status_code == 201

    def test_handshake_authorized_raw(self, client, webhook_secret):
        payload = {"type": "verification"}
        response = client.post(
            "/webhooks/google-health",
            json=payload,
            headers={"Authorization": webhook_secret},
        )
        assert response.status_code == 201

    def test_handshake_unauthorized_wrong_secret(self, client):
        payload = {"type": "verification"}
        response = client.post(
            "/webhooks/google-health",
            json=payload,
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert response.status_code == 401

    def test_handshake_unauthorized_missing_auth(self, client):
        payload = {"type": "verification"}
        response = client.post("/webhooks/google-health", json=payload)
        assert response.status_code == 401

    def test_notification_missing_signature(self, client):
        payload = {
            "data": {
                "version": "1",
                "clientProvidedSubscriptionName": "sub123",
                "healthUserId": "user123",
                "operation": "UPSERT",
                "dataType": "weight",
            }
        }
        response = client.post("/webhooks/google-health", json=payload)
        assert response.status_code == 401
        assert response.text == "Missing signature"

    def test_notification_invalid_signature(self, client):
        payload = {
            "data": {
                "version": "1",
                "clientProvidedSubscriptionName": "sub123",
                "healthUserId": "user123",
                "operation": "UPSERT",
                "dataType": "weight",
            }
        }
        response = client.post(
            "/webhooks/google-health",
            json=payload,
            headers={"GOOGLE-HEALTH-API-SIGNATURE": "invalid-sig-base64"},
        )
        assert response.status_code == 401
        assert response.text == "Invalid signature"

    def test_notification_valid_signature_unmatched_user(self, client, tink_keys, mock_db, mocker):
        payload = {
            "data": {
                "version": "1",
                "clientProvidedSubscriptionName": "sub123",
                "healthUserId": "unmatched-user-123",
                "operation": "UPSERT",
                "dataType": "weight",
            }
        }
        payload_bytes = json.dumps(payload).encode()

        # Sign the payload using private key
        signer = tink_keys["private_handle"].primitive(signature.PublicKeySign)
        sig = signer.sign(payload_bytes)
        sig_b64 = base64.b64encode(sig).decode()

        # Mock public keyset fetching
        mocker.patch("src.routes.webhooks._get_public_keyset", return_value=tink_keys["public_json"])

        # Mock Firestore user query returning None
        mock_db.get_user_id_by_health_user_id.return_value = None

        response = client.post(
            "/webhooks/google-health",
            content=payload_bytes,
            headers={"GOOGLE-HEALTH-API-SIGNATURE": sig_b64},
        )
        assert response.status_code == 204
        mock_db.get_user_id_by_health_user_id.assert_called_once_with("unmatched-user-123")

    def test_notification_valid_signature_success(self, client, tink_keys, mock_db, mock_sync_orchestrator, mocker):
        payload = {
            "data": {
                "version": "1",
                "clientProvidedSubscriptionName": "sub123",
                "healthUserId": "google-user-123",
                "operation": "UPSERT",
                "dataType": "weight",
            }
        }
        payload_bytes = json.dumps(payload).encode()

        # Sign the payload using private key
        signer = tink_keys["private_handle"].primitive(signature.PublicKeySign)
        sig = signer.sign(payload_bytes)
        sig_b64 = base64.b64encode(sig).decode()

        # Mock public keyset fetching
        mocker.patch("src.routes.webhooks._get_public_keyset", return_value=tink_keys["public_json"])

        # Mock database matches google-user-123 to local-user-999
        mock_db.get_user_id_by_health_user_id.return_value = "local-user-999"

        response = client.post(
            "/webhooks/google-health",
            content=payload_bytes,
            headers={"GOOGLE-HEALTH-API-SIGNATURE": sig_b64},
        )
        assert response.status_code == 204
        mock_db.get_user_id_by_health_user_id.assert_called_once_with("google-user-123")

        # Give it a moment to run the background task queue, or verify the mock sync_orchestrator was called
        # Since FastAPI's TestClient runs background tasks synchronously before returning, we can assert directly
        mock_sync_orchestrator.sync_user.assert_called_once_with("local-user-999")

    def test_notification_list_payload_valid_signature_success(
        self, client, tink_keys, mock_db, mock_sync_orchestrator, mocker
    ):
        payload = [
            {
                "data": {
                    "version": "1",
                    "clientProvidedSubscriptionName": "sub123",
                    "healthUserId": "google-user-456",
                    "operation": "UPSERT",
                    "dataType": "weight",
                }
            }
        ]
        payload_bytes = json.dumps(payload).encode()

        # Sign the payload using private key
        signer = tink_keys["private_handle"].primitive(signature.PublicKeySign)
        sig = signer.sign(payload_bytes)
        sig_b64 = base64.b64encode(sig).decode()

        # Mock public keyset fetching
        mocker.patch("src.routes.webhooks._get_public_keyset", return_value=tink_keys["public_json"])

        # Mock database matches google-user-456 to local-user-777
        mock_db.get_user_id_by_health_user_id.return_value = "local-user-777"

        response = client.post(
            "/webhooks/google-health",
            content=payload_bytes,
            headers={"GOOGLE-HEALTH-API-SIGNATURE": sig_b64},
        )
        assert response.status_code == 204
        mock_db.get_user_id_by_health_user_id.assert_called_with("google-user-456")
        mock_sync_orchestrator.sync_user.assert_called_with("local-user-777")
