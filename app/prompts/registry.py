"""Load a versioned prompt and decide which one is active.

An unknown id falls back to the latest stable version for that family.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from app.config import get_settings


_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptVersion:
    id: str
    family: str
    number: int
    changelog: str
    created: str
    stable: bool
    text: str
    update_cycle_description: Optional[str] = None
    shift_by_days_description: Optional[str] = None


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _tool_text(relative: Optional[str]) -> Optional[str]:
    if not relative:
        return None
    return _read(relative).strip()


def _version(
    version_id: str,
    family: str,
    number: int,
    changelog: str,
    created: str,
    stable: bool,
    path: str,
    tool_path: Optional[str] = None,
    shift_path: Optional[str] = None,
) -> PromptVersion:
    return PromptVersion(
        id=version_id,
        family=family,
        number=number,
        changelog=changelog,
        created=created,
        stable=stable,
        text=_read(path),
        update_cycle_description=_tool_text(tool_path),
        shift_by_days_description=_tool_text(shift_path),
    )


# chat-v2 is the stable default. Roll back with CHAT_PROMPT_VERSION=chat-v1.
_VERSIONS = (
    _version(
        "chat-v1",
        "chat",
        1,
        "Chat system prompt after the onboarding anchor and narration fixes.",
        "2026-10-22",
        True,
        "chat/v1.md",
    ),
    _version(
        "chat-v2",
        "chat",
        2,
        "State that a positive shift_by_days moves each shift later.",
        "2026-10-22",
        True,
        "chat/v2.md",
        tool_path="chat/v2_update_cycle.txt",
        shift_path="chat/v2_shift_by_days.txt",
    ),
    _version(
        "onboarding-v1",
        "onboarding",
        1,
        "Onboarding rotation-parse system prompt as it shipped when versioning was added.",
        "2026-10-22",
        True,
        "onboarding/v1.md",
    ),
)

VERSIONS = {item.id: item for item in _VERSIONS}


def load_version(version_id: str) -> PromptVersion:
    """The named version. Raises KeyError when the id is not in the registry."""
    return VERSIONS[version_id]


def latest_stable(family: str) -> PromptVersion:
    stable = [item for item in _VERSIONS if item.family == family and item.stable]
    if not stable:
        raise RuntimeError(f"No stable prompt for {family}")
    return max(stable, key=lambda item: item.number)


def _parse_allowlist(raw: str) -> dict:
    found = {}
    for part in (raw or "").split(","):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        user_id, version_id = piece.split(":", 1)
        user_id = user_id.strip()
        version_id = version_id.strip()
        if user_id and version_id:
            found[user_id] = version_id
    return found


def _resolve(family: str, version_id: Optional[str]) -> PromptVersion:
    fallback = latest_stable(family)
    chosen = (version_id or "").strip()
    if not chosen:
        return fallback
    found = VERSIONS.get(chosen)
    if found is None or found.family != family:
        logger.warning(
            f"Unknown {family} prompt version {chosen!r}; using {fallback.id}"
        )
        return fallback
    return found


def version_for(family: str, user_id: Optional[str] = None) -> PromptVersion:
    """Active version for this family.

    A user on the allowlist uses that entry. Otherwise the env version
    is used. An empty env value is the latest stable version.
    """
    settings = get_settings()
    allow_raw = getattr(settings, f"{family}_prompt_allowlist", "") or ""
    allow = _parse_allowlist(allow_raw)
    if user_id and str(user_id) in allow:
        return _resolve(family, allow[str(user_id)])
    chosen = getattr(settings, f"{family}_prompt_version", "") or ""
    return _resolve(family, chosen)


def missing_prompt_column(exc: Exception) -> bool:
    """True when Postgres or PostgREST rejected a prompt_version column."""
    code = str(getattr(exc, "code", "") or "")
    text = " ".join(
        str(part) for part in (
            exc,
            getattr(exc, "message", ""),
            getattr(exc, "details", ""),
        )
    ).lower()
    if "prompt_version" not in text:
        return False
    return code == "PGRST204" or "column" in text or "schema cache" in text


def insert_row(client, table: str, data: dict):
    """Insert a row. Retry without prompt_version when that column is absent.

    The version is still logged, and callers also keep it in JSON metadata.
    """
    version = data.get("prompt_version")
    if version:
        logger.info(f"{table} prompt_version={version}")
    try:
        return client.table(table).insert(data).execute()
    except Exception as exc:
        if not version or not missing_prompt_column(exc):
            raise
        logger.warning(
            f"{table}.prompt_version is not in the database yet; "
            f"logged {version} and left it in the row JSON"
        )
        slim = {key: value for key, value in data.items() if key != "prompt_version"}
        return client.table(table).insert(slim).execute()
