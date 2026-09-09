"""Server-established identities; never deserialize a principal from a request."""

import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass

from .errors import DemoError


@dataclass(frozen=True)
class Principal:
    id: str
    display_name: str
    role: str = "user"

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id) or self.role not in ("user", "admin"):
            raise ValueError("Invalid configured identity.")
        if not isinstance(self.display_name, str) or not 1 <= len(self.display_name) <= 100:
            raise ValueError("Invalid configured display name.")

    @property
    def storage_key(self) -> str:
        return hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:24]

    def as_dict(self):
        return {"id": self.id, "display_name": self.display_name, "role": self.role}


LOCAL_OPERATOR = Principal("legacy", "Trusted local operator", "admin")
current_principal: ContextVar[Principal | None] = ContextVar("principal", default=None)


def authenticated_principal() -> Principal:
    principal = current_principal.get()
    if principal is None:
        raise DemoError("AUTH_REQUIRED", "Authenticate using your individual access credential.")
    return principal
