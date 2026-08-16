"""Local operator CLI for identity and API-key lifecycle."""

from __future__ import annotations

import argparse
import getpass

from nanodelta.api.runtime import _connect
from nanodelta.security import PostgresSecurityStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage NanoDelta durable identities")
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("upsert-user")
    user.add_argument("username")
    user.add_argument("--role", choices=("viewer", "operator", "admin"), required=True)
    disabled = commands.add_parser("disable-user")
    disabled.add_argument("username")
    key = commands.add_parser("create-api-key")
    key.add_argument("name")
    key.add_argument("--actor-id", required=True)
    key.add_argument("--role", choices=("viewer", "operator", "admin"), required=True)
    revoke = commands.add_parser("revoke-api-key")
    revoke.add_argument("key_id")
    args = parser.parse_args()
    store = PostgresSecurityStore(_connect)
    if args.command == "upsert-user":
        password = getpass.getpass("Password (minimum 12 characters): ")
        print(store.upsert_user(args.username, password, args.role))
    elif args.command == "disable-user":
        if not store.disable_user(args.username):
            raise SystemExit("user not found")
    elif args.command == "create-api-key":
        key_id, raw = store.create_api_key(args.name, args.actor_id, args.role)
        print(f"key_id={key_id}\napi_key={raw}\nStore this key now; it cannot be recovered.")
    elif not store.revoke_api_key(args.key_id, "local-operator"):
        raise SystemExit("active API key not found")
