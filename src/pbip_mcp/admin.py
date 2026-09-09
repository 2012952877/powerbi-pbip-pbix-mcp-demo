"""Local operator commands. Credentials are written only to restricted files."""

import argparse
import json
import secrets
import re
from pathlib import Path

from .auth import digest, private_file
from .config import Config
from .errors import DemoError
from .identity import Principal
from .storage import JobStore, atomic_json


def add_user(config: Path, credential_file: Path, user: Principal) -> dict:
    if user.id == "legacy":
        raise DemoError("AUTH_CONFIG", "legacy is reserved for the trusted local operator.")
    if credential_file.exists():
        raise DemoError("OUTPUT_EXISTS", "Credential delivery file already exists.")
    if config.exists():
        private_file(config)
        value = json.loads(config.read_text(encoding="utf-8"))
    else:
        value = {"version": 1, "users": []}
    if any(record["id"] == user.id for record in value["users"]):
        raise DemoError("AUTH_CONFIG", "That user already exists; edit or rotate through the operator workflow.")
    # Restrict new dedicated parent folders before writing any credential.
    for parent in {config.parent, credential_file.parent}:
        if not parent.exists():
            parent.mkdir(parents=True)
            private_file(parent, create=True)
        else:
            private_file(parent)
    credential = secrets.token_urlsafe(32)
    value["users"].append({**user.as_dict(), "token_sha256": digest(credential)})
    with credential_file.open("x", encoding="utf-8") as stream:
        stream.write(credential)
    private_file(credential_file, create=True)
    atomic_json(config, value)
    private_file(config, create=True)
    return {"ok": True, "user": user.as_dict(), "credential_written": True}

def register_digest(config: Path, user: Principal, token_hash: str) -> dict:
    private_file(config)
    if user.id == "legacy" or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
        raise DemoError("AUTH_CONFIG", "Supply a valid identity and SHA-256 of an independently generated high-entropy token.")
    value = json.loads(config.read_text(encoding="utf-8"))
    if any(record["id"] == user.id or record["token_sha256"] == token_hash for record in value["users"]):
        raise DemoError("AUTH_CONFIG", "That identity or credential already exists.")
    value["users"].append({**user.as_dict(), "token_sha256": token_hash})
    atomic_json(config, value)
    private_file(config, create=True)
    return {"ok": True, "user": user.as_dict(), "restart_required": True}


def remove_user(config: Path, user_id: str) -> dict:
    private_file(config)
    value = json.loads(config.read_text(encoding="utf-8"))
    users = [record for record in value["users"] if record["id"] != user_id]
    if len(users) == len(value["users"]):
        raise DemoError("AUTH_CONFIG", "No configured user has that identifier.")
    value["users"] = users
    atomic_json(config, value)
    private_file(config, create=True)
    return {"ok": True, "removed_user_id": user_id, "restart_required": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    add = commands.add_parser("add-user")
    add.add_argument("--auth-config", required=True, type=Path)
    add.add_argument("--credential-file", required=True, type=Path)
    add.add_argument("--user-id", required=True)
    add.add_argument("--display-name", required=True)
    add.add_argument("--role", choices=("user", "admin"), default="user")
    register = commands.add_parser("register-digest")
    register.add_argument("--auth-config", required=True, type=Path)
    register.add_argument("--user-id", required=True)
    register.add_argument("--display-name", required=True)
    register.add_argument("--token-sha256", required=True)
    remove = commands.add_parser("remove-user")
    remove.add_argument("--auth-config", required=True, type=Path)
    remove.add_argument("--user-id", required=True)
    for name in ("status", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "add-user":
            result = add_user(args.auth_config.resolve(), args.credential_file.resolve(),
                              Principal(args.user_id, args.display_name, args.role))
        elif args.action == "register-digest":
            result = register_digest(args.auth_config.resolve(), Principal(args.user_id, args.display_name), args.token_sha256)
        elif args.action == "remove-user":
            result = remove_user(args.auth_config.resolve(), args.user_id)
        else:
            store = JobStore(Config(args.data_dir.resolve()))
            result = {"ok": True, **(store.cleanup_expired() if args.action == "cleanup" else
                                     {"worker": store.worker_status(), "queue": store.queue_status()})}
    except DemoError as exc:
        result = {"ok": False, "error": exc.as_dict()}
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
