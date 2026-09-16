"""Installed backend identities, capabilities, and private logical task keys."""

from dataclasses import dataclass
from typing import Literal

BUILTIN_TASK_PREFIX = "mship-builtin-"


@dataclass(frozen=True)
class BuiltinBackend:
    name: str
    module: str
    operations: tuple[str, ...]
    session_owner: Literal["android", "flutter"] | None = None

    def task_key(self, operation: str) -> str:
        return f"{BUILTIN_TASK_PREFIX}{self.name}-{operation}"


BUILTIN_BACKENDS = {
    "android": BuiltinBackend(
        "android",
        "mship.backends.android.backend",
        ("run", "logs", "capture"),
        "android",
    ),
    "flutter": BuiltinBackend(
        "flutter",
        "mship.backends.flutter.backend",
        ("run", "logs", "capture", "reload", "restart"),
        "flutter",
    ),
    "ios": BuiltinBackend(
        "ios", "mship.backends.ios.backend", ("run", "logs", "capture")
    ),
    "browser": BuiltinBackend(
        "browser", "mship.backends.browser.backend", ("run", "logs", "capture")
    ),
    "platformio": BuiltinBackend(
        "platformio", "mship.backends.platformio.backend", ("run", "logs", "upload")
    ),
}
