#!/usr/bin/env python3
"""Register, update, delete, or check status of a Google Health API subscriber (webhook)."""

import argparse
import sys

import google.auth
import httpx
from google.auth.transport.requests import Request as AuthRequest


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(
        description="Register, delete, or check status of a Google Health API webhook subscriber."
    )
    parser.add_argument("--project-number", required=True, help="Google Cloud Project NUMBER (not ID!)")
    parser.add_argument(
        "--webhook-url",
        help="Full URL of the webhook (e.g. https://domain.com/webhooks/google-health)",
    )
    parser.add_argument("--webhook-secret", help="Shared secret used for webhook authorization verification")
    parser.add_argument(
        "--subscriber-id", default="garth-relay-webhook", help="Subscriber ID (default: garth-relay-webhook)"
    )
    parser.add_argument("--delete", action="store_true", help="Delete/unregister the subscriber instead of registering")
    parser.add_argument(
        "--status", action="store_true", help="Check/show the status of the subscriber and its subscriptions"
    )

    args = parser.parse_args()

    if not (args.delete or args.status):
        if not args.webhook_url:
            parser.error("--webhook-url is required for registration")
        if not args.webhook_secret:
            parser.error("--webhook-secret is required for registration")

    # Authenticate using Application Default Credentials
    print("Authenticating with Google Cloud...")
    try:
        credentials, project_id = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(AuthRequest())
        access_token = credentials.token
        quota_project = getattr(credentials, "quota_project_id", None) or project_id
    except Exception as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        print("Please run 'gcloud auth application-default login' first.", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if quota_project:
        headers["X-Goog-User-Project"] = quota_project

    if args.delete:
        url = f"https://health.googleapis.com/v4/projects/{args.project_number}/subscribers/{args.subscriber_id}?force=true"
        print("Sending delete request to Google Health API...")
        print(f"Subscriber ID: {args.subscriber_id}")

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.delete(url, headers=headers)

                if resp.status_code in (200, 204):
                    print("\nSuccess! Webhook subscriber deleted/unregistered successfully.")
                else:
                    print(f"\nError: Webhook deletion failed with status code {resp.status_code}", file=sys.stderr)
                    print(resp.text, file=sys.stderr)
                    sys.exit(1)
        except Exception as e:
            print(f"\nFailed to connect to Google Health API: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.status:
        subscriber_url = (
            f"https://health.googleapis.com/v4/projects/{args.project_number}/subscribers/{args.subscriber_id}"
        )
        subscriptions_url = f"https://health.googleapis.com/v4/projects/{args.project_number}/subscribers/{args.subscriber_id}/subscriptions"

        print("Checking subscriber status with Google Health API...")
        print(f"Subscriber ID: {args.subscriber_id}\n")

        try:
            with httpx.Client(timeout=30.0) as client:
                # 1. Fetch Subscriber details
                sub_resp = client.get(subscriber_url, headers=headers)
                if sub_resp.status_code == 404:
                    print(f"Subscriber '{args.subscriber_id}' is NOT registered in project '{args.project_number}'.")
                    sys.exit(0)
                elif sub_resp.status_code not in (200, 201):
                    print(f"Error fetching subscriber details (status {sub_resp.status_code}):", file=sys.stderr)
                    print(sub_resp.text, file=sys.stderr)
                    sys.exit(1)

                sub_data = sub_resp.json()
                print("=== Subscriber Details ===")
                print(f"Name: {sub_data.get('name')}")
                print(f"Endpoint URL: {sub_data.get('endpointUri')}")
                print("Configs:")
                for cfg in sub_data.get("subscriberConfigs", []):
                    print(f"  - Data Types: {cfg.get('dataTypes')}")
                    print(f"    Create Policy: {cfg.get('subscriptionCreatePolicy')}")
                print("==========================\n")

                # 2. Fetch active Subscriptions list
                print("Fetching active subscriptions...")
                subs_resp = client.get(subscriptions_url, headers=headers)
                if subs_resp.status_code not in (200, 201):
                    print(f"Error fetching active subscriptions (status {subs_resp.status_code}):", file=sys.stderr)
                    print(subs_resp.text, file=sys.stderr)
                    sys.exit(1)

                subs_data = subs_resp.json()
                subscriptions = subs_data.get("subscriptions", [])
                if not subscriptions:
                    print("No active user subscriptions found under this subscriber.")
                else:
                    print(f"Found {len(subscriptions)} active subscription(s):")
                    for i, sub in enumerate(subscriptions, 1):
                        print(f"  Subscription {i}:")
                        print(f"    Name: {sub.get('name')}")
                        print(f"    Data Type: {sub.get('dataType')}")
                        print(f"    State: {sub.get('state')}")
                        print(f"    Update Time: {sub.get('updateTime')}")
        except Exception as e:
            print(f"\nFailed to connect to Google Health API: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        url = f"https://health.googleapis.com/v4/projects/{args.project_number}/subscribers?subscriberId={args.subscriber_id}"
        body = {
            "endpointUri": args.webhook_url,
            "subscriberConfigs": [
                {
                    "dataTypes": ["weight", "body-fat"],
                    "subscriptionCreatePolicy": "AUTOMATIC",
                }
            ],
            "endpointAuthorization": {"secret": args.webhook_secret},
        }

        print("Sending registration request to Google Health API...")
        print(f"Endpoint URL: {args.webhook_url}")
        print(f"Subscriber ID: {args.subscriber_id}")

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=headers, json=body)

                if resp.status_code in (200, 201):
                    print("\nSuccess! Webhook subscriber registered successfully.")
                    print(resp.json())
                else:
                    print(f"\nError: Webhook registration failed with status code {resp.status_code}", file=sys.stderr)
                    print(resp.text, file=sys.stderr)
                    sys.exit(1)
        except Exception as e:
            print(f"\nFailed to connect to Google Health API: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
