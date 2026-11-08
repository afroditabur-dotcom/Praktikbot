"""Модуль авторизации сотрудников ПрактикБота.

Коды выдаются локальным оператором командой:
    python security.py issue teacher|employer|admin

Хранится только SHA-256-хэш кода. Сам код показывается один раз при выпуске.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# Режим безопасности задаётся только .env / переменными окружения.
# Поле testing_auth_mode в report_structure.json — устаревшая подсказка схемы, не переключатель.
BASE_DIR = Path(__file__).resolve().parent
SECURITY_ENABLED = os.getenv("SECURITY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off", "выкл"
}
TESTING_AUTH_MODE = os.getenv("TESTING_AUTH_MODE", "false").strip().lower() in {
    "1", "true", "yes", "on", "да"
}
ACCESS_CODE_TTL_SECONDS = 24 * 60 * 60
AUTH_MAX_ATTEMPTS = 5
AUTH_ATTEMPT_WINDOW_SECONDS = 15 * 60
AUTH_BLOCK_SECONDS = 15 * 60
STAFF_ROLES = {"teacher", "employer", "admin"}
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_code(value: Any) -> str:
    """Удаляет пробелы/дефисы и приводит код к верхнему регистру."""
    return "".join(str(value or "").upper().split()).replace("-", "")


def hash_code(value: Any) -> str:
    normalized = normalize_code(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def generate_access_code(length: int = 16) -> str:
    """Генерирует код без визуально неоднозначных символов."""
    if not 8 <= length <= 32:
        raise ValueError("Длина кода должна быть от 8 до 32 символов.")
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def create_access_code(data: dict[str, Any], role: str, now: int | None = None) -> tuple[str, dict[str, Any]]:
    """Создаёт одноразовый код для конкретной роли сроком на 24 часа."""
    if role not in STAFF_ROLES:
        raise ValueError("Код можно выпустить только для teacher, employer или admin.")
    timestamp = int(time.time() if now is None else now)
    code = generate_access_code()
    access_codes = data.setdefault("access_codes", [])
    if not isinstance(access_codes, list):
        raise TypeError("Поле access_codes должно быть списком.")
    while any(
        isinstance(item, dict)
        and hmac.compare_digest(str(item.get("code_hash", "")), hash_code(code))
        for item in access_codes
    ):
        code = generate_access_code()
    row = {
        "code_id": "AC-" + secrets.token_hex(4).upper(),
        "role": role,
        "code_hash": hash_code(code),
        "created_at": timestamp,
        "expires_at": timestamp + ACCESS_CODE_TTL_SECONDS,
        "used_at": None,
        "used_by": None,
        "revoked_at": None,
        "is_active": True,
    }
    access_codes.append(row)
    return code, row


def migrate_access_codes(data: dict[str, Any], now: int | None = None) -> bool:
    """Переводит старые записи с открытым кодом на хэшированное хранение.

    Старые записи без даты создания получают новый 24-часовой срок с момента
    миграции: это не позволяет бессрочно использовать неизвестно когда созданные коды.
    """
    timestamp = int(time.time() if now is None else now)
    changed = False
    codes = data.setdefault("access_codes", [])
    if isinstance(codes, dict):
        converted = []
        for key, value in codes.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("code", str(key))
            else:
                item = {"code": str(key), "role": str(value), "is_active": True}
            converted.append(item)
        codes = data["access_codes"] = converted
        changed = True
    if not isinstance(codes, list):
        data["access_codes"] = []
        return True

    for row in codes:
        if not isinstance(row, dict):
            continue
        plaintext = row.pop("code", None)
        if plaintext is not None:
            changed = True
        if plaintext and not row.get("code_hash"):
            row["code_hash"] = hash_code(plaintext)
            changed = True
        if row.get("code_hash"):
            created_at = _safe_int(row.get("created_at"), 0)
            if created_at <= 0:
                created_at = timestamp
                row["created_at"] = created_at
                changed = True
            expires_at = _safe_int(row.get("expires_at"), 0)
            if expires_at <= 0:
                row["expires_at"] = created_at + ACCESS_CODE_TTL_SECONDS
                changed = True
        if row.get("code_hash"):
            if "is_active" not in row:
                row["is_active"] = True
                changed = True
            row.setdefault("used_at", None)
            row.setdefault("used_by", None)
            row.setdefault("revoked_at", None)
            if not row.get("code_id"):
                row["code_id"] = "AC-" + secrets.token_hex(4).upper()
                changed = True
    return changed


def _record_failed_attempt(data: dict[str, Any], uid: str, now: int) -> str:
    attempts = data.setdefault("auth_attempts", {})
    if not isinstance(attempts, dict):
        attempts = data["auth_attempts"] = {}
    row = attempts.setdefault(str(uid), {})
    if not isinstance(row, dict):
        row = attempts[str(uid)] = {}
    blocked_until = _safe_int(row.get("blocked_until", 0), 0)
    if now < blocked_until:
        return "blocked"
    window_start = _safe_int(row.get("window_start", now), now) or now
    count = _safe_int(row.get("count", 0), 0)
    if now - window_start >= AUTH_ATTEMPT_WINDOW_SECONDS:
        window_start, count = now, 0
    count += 1
    row.update({"window_start": window_start, "count": count})
    if count >= AUTH_MAX_ATTEMPTS:
        row["blocked_until"] = now + AUTH_BLOCK_SECONDS
        return "blocked"
    return "invalid"


def verify_and_consume_access_code(
    data: dict[str, Any], uid: Any, role: str, supplied_code: str, now: int | None = None
) -> tuple[bool, str]:
    """Проверяет код и при успехе атомарно отмечает его использованным в данных.

    Возвращаемые причины: ok, invalid, expired, blocked, wrong_role.
    """
    timestamp = int(time.time() if now is None else now)
    user_id = str(uid)
    if role not in STAFF_ROLES:
        return False, "invalid"
    attempts = data.setdefault("auth_attempts", {})
    attempt = attempts.get(user_id, {}) if isinstance(attempts, dict) else {}
    if isinstance(attempt, dict) and timestamp < _safe_int(attempt.get("blocked_until", 0), 0):
        return False, "blocked"

    normalized = normalize_code(supplied_code)
    if not normalized:
        reason = _record_failed_attempt(data, user_id, timestamp)
        return False, reason
    candidate_hash = hash_code(normalized)
    matched_any = False
    matched_role = False
    expired = False

    for row in data.get("access_codes", []) or []:
        if not isinstance(row, dict) or not row.get("code_hash"):
            continue
        if not hmac.compare_digest(str(row.get("code_hash", "")), candidate_hash):
            continue
        matched_any = True
        if row.get("role") != role:
            continue
        matched_role = True
        if row.get("used_at") or row.get("revoked_at") or row.get("is_active") is False:
            continue
        if timestamp >= _safe_int(row.get("expires_at", 0), 0):
            expired = True
            continue
        row["used_at"] = timestamp
        row["used_by"] = user_id
        row["is_active"] = False
        data.setdefault("staff_auth", {})[user_id] = {
            "role": role,
            "max_user_id": user_id,
            "authorized_at": timestamp,
            "access_code_id": row.get("code_id"),
            "auth_method": "access_code",
            "active": True,
        }
        data.setdefault("auth_attempts", {}).pop(user_id, None)
        return True, "ok"

    reason = _record_failed_attempt(data, user_id, timestamp)
    if reason == "blocked":
        return False, "blocked"
    if expired and matched_role:
        return False, "expired"
    if matched_any and not matched_role:
        return False, "wrong_role"
    return False, "invalid"


def is_staff_authorized(uid: Any, role: str, data: dict[str, Any]) -> bool:
    """Проверяет постоянную авторизацию по MAX user_id, не срок кода."""
    if role not in STAFF_ROLES:
        return False
    record = data.get("staff_auth", {}).get(str(uid), {})
    return bool(
        isinstance(record, dict)
        and record.get("active") is True
        and record.get("auth_method") == "access_code"
        and record.get("role") == role
        and str(record.get("max_user_id")) == str(uid)
    )


def deactivate_staff_auth(data: dict[str, Any], uid: Any, now: int | None = None) -> dict[str, Any] | None:
    """Отзывает авторизацию, сохраняя запись об отзыве для аудита."""
    user_id = str(uid)
    record = data.get("staff_auth", {}).get(user_id)
    if not isinstance(record, dict):
        return None
    timestamp = int(time.time() if now is None else now)
    record["active"] = False
    record["revoked_at"] = timestamp
    history = data.setdefault("staff_auth_history", [])
    history.append({**record, "max_user_id": user_id})
    return record


def _data_file() -> Path:
    raw = os.getenv("DATA_FILE", "bot_data.json").strip() or "bot_data.json"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _load_cli_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"access_codes": [], "auth_attempts": {}, "staff_auth": {}, "staff_auth_history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Корень bot_data.json должен быть JSON-объектом; файл не изменён.")
    if not isinstance(data.get("access_codes", []), (list, dict)):
        raise RuntimeError("Поле access_codes имеет неподдерживаемый формат; файл не изменён.")
    migrate_access_codes(data)
    return data


def _save_cli_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".security.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Управление одноразовыми кодами доступа ПрактикБота")
    parser.add_argument("--data-file", help="путь к bot_data.json (например, data/bot_data.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="выпустить новый код доступа на 24 часа")
    issue.add_argument("role", choices=sorted(STAFF_ROLES))
    revoke = sub.add_parser("revoke", help="отозвать ещё не использованный код по его ID")
    revoke.add_argument("code_id")
    sub.add_parser("list", help="показать состояние выпущенных кодов без самих кодов")
    args = parser.parse_args()
    path = Path(args.data_file).expanduser() if args.data_file else _data_file()
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        data = _load_cli_data(path)
        if args.command == "issue":
            code, row = create_access_code(data, args.role)
            _save_cli_data(path, data)
            expires = datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat(timespec="seconds")
            print("Код выпущен. Передайте его сотруднику лично или по защищённому каналу.")
            print(f"Роль: {args.role}")
            formatted_code = "-".join(code[i:i + 4] for i in range(0, len(code), 4))
            print(f"Код (показывается только сейчас): {formatted_code}")
            print(f"ID кода: {row['code_id']}")
            print(f"Действителен до (UTC): {expires}")
            print("Не отправляйте этот код в общий чат и не сохраняйте его в открытом виде.")
            return 0
        if args.command == "revoke":
            row = next((x for x in data.get("access_codes", []) if x.get("code_id") == args.code_id), None)
            if not row:
                print("Код с таким ID не найден.")
                return 1
            if row.get("used_at"):
                print("Код уже использован; отозвать его повторно нельзя.")
                return 1
            row["is_active"] = False
            row["revoked_at"] = int(time.time())
            _save_cli_data(path, data)
            print("Код отозван.")
            return 0
        if args.command == "list":
            for row in data.get("access_codes", []):
                expires = datetime.fromtimestamp(int(row.get("expires_at", 0)), timezone.utc).isoformat(timespec="seconds") if row.get("expires_at") else "—"
                if row.get("used_at"):
                    status = "использован"
                elif row.get("revoked_at") or row.get("is_active") is False:
                    status = "отозван"
                elif _safe_int(row.get("expires_at", 0), 0) <= int(time.time()):
                    status = "истёк"
                else:
                    status = "активен"
                print(f"{row.get('code_id', 'без ID')} | {row.get('role', '?')} | {status} | до {expires}")
            return 0
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
