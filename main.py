# ПрактикБот — MAX Bot API
# Совместим с report_structure.json старого формата и с расширенным форматом проекта.
# Python 3.10+; настройки: BOT_TOKEN, необязательно GROUPS в .env.

from __future__ import annotations

import json
import os
import queue
import importlib.util
import io
import html
import re
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# На Windows подключаем системное хранилище сертификатов ДО импорта requests.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

load_dotenv()

import security as security_manager

SECURITY_ENABLED = security_manager.SECURITY_ENABLED
TESTING_AUTH_MODE = security_manager.TESTING_AUTH_MODE
STAFF_ROLES = {"teacher", "employer", "admin"}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_URL = "https://platform-api2.max.ru"
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = Path(os.getenv("DATA_FILE", str(BASE_DIR / "bot_data.json"))).expanduser()
if not DATA_FILE.is_absolute():
    DATA_FILE = BASE_DIR / DATA_FILE
FLOW_CANDIDATES = (
    BASE_DIR / "report_structure.json",
    BASE_DIR / "report_structure(5).json",
    BASE_DIR / "report_structure(4).json",
    BASE_DIR / "report_structure(2).json",
)
_flow_env = os.getenv("FLOW_FILE", "").strip()
FLOW_FILE = (Path(_flow_env) if _flow_env else next((p for p in FLOW_CANDIDATES if p.exists()), FLOW_CANDIDATES[0]))
if not FLOW_FILE.is_absolute():
    FLOW_FILE = BASE_DIR / FLOW_FILE
HEADERS = {"Authorization": BOT_TOKEN}

PRACTICES = [f"PM_{i:02d}" for i in range(1, 9)]
REPORT_PRACTICES = {"PM_01", "PM_02", "PM_04", "PM_05"}
DIARY_PRACTICES = set(PRACTICES) - REPORT_PRACTICES
ATTACHMENT_SECTIONS = {"1", "2", "4"}

try:
    FLOW: dict[str, Any] = json.loads(FLOW_FILE.read_text(encoding="utf-8"))
    if not isinstance(FLOW, dict):
        raise ValueError("корень JSON должен быть объектом")
except Exception as exc:
    raise RuntimeError(f"Не удалось прочитать {FLOW_FILE.name}: {exc}") from exc

STUDENT_FLOW = FLOW.get("simplified_student_flow", {})
TEACHER_FLOW = FLOW.get("teacher_flow", {})
EMPLOYER_FLOW = FLOW.get("employer_flow", {})
ADMIN_FLOW = FLOW.get("admin_flow", {})

# В старом JSON полный список групп отсутствует. Его можно задать в .env:
# GROUPS=ЭЛС-21,ЭЛС-22,ЭЛС-23
_common_flow = FLOW.get("common_flow", {}) if isinstance(FLOW.get("common_flow", {}), dict) else {}
_json_groups = (FLOW.get("groups") or FLOW.get("college_groups") or FLOW.get("student_groups")
                or STUDENT_FLOW.get("groups") or _common_flow.get("groups"))
_env_groups = os.getenv("GROUPS", "").strip()
if _env_groups:
    GROUPS = [x.strip() for x in _env_groups.split(",") if x.strip()]
elif isinstance(_json_groups, list) and _json_groups:
    GROUPS = [str(x).strip() for x in _json_groups if str(x).strip()]
else:
    GROUPS = ["ЭЛС-21", "ЭЛС-22"]

analyze_work_description = None
format_work_description = None
answer_student_question = None
AI_CONFIGURED = False
try:
    from ai_assistant import analyze_work_description, format_work_description, answer_student_question, AI_CONFIGURED
except Exception as first_ai_error:
    # Совместимость с файлами, скачанными под именем ai_assistant(1).py.
    for candidate in (BASE_DIR / "ai_assistant.py", BASE_DIR / "ai_assistant(1).py"):
        if candidate.exists():
            try:
                spec = importlib.util.spec_from_file_location("practicebot_ai_assistant", candidate)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                analyze_work_description = getattr(module, "analyze_work_description", None)
                format_work_description = getattr(module, "format_work_description", None)
                answer_student_question = getattr(module, "answer_student_question", None)
                AI_CONFIGURED = bool(getattr(module, "AI_CONFIGURED", False))
                if analyze_work_description or format_work_description:
                    break
            except Exception as exc:
                print(f"Не удалось подключить {candidate.name}: {type(exc).__name__}")
    if analyze_work_description is None and format_work_description is None:
        print(f"ИИ-дополнение отключено: {type(first_ai_error).__name__}")

HTTP = requests.Session()
DATA_LOCK = threading.RLock()
AI_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="practice-ai")
CALLBACK_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="max-callback-ack")
AI_RESULTS: queue.Queue[tuple[str, str, str, dict[str, Any]]] = queue.Queue()
AI_HELP_RESULTS: queue.Queue[tuple[str, str, str]] = queue.Queue()
_SEND_LOCK = threading.Lock()
_SEND_LOCKS: dict[str, threading.Lock] = {}
_LAST_SEND_AT: dict[str, float] = {}


def _throttle_user(uid: Any, interval: float = 0.55) -> None:
    """Соблюдает лимит MAX: не более двух сообщений в секунду в один диалог."""
    key = str(uid)
    with _SEND_LOCK:
        lock = _SEND_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        wait = interval - (now - _LAST_SEND_AT.get(key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_SEND_AT[key] = time.monotonic()


def _empty_data() -> dict[str, Any]:
    return {
        "students": {}, "teachers": {}, "employers": {}, "admins": {},
        "users": {}, "states": {}, "practice_reports": {}, "practice_diary": {},
        "temporary": {}, "auth_attempts": {}, "access_codes": [], "links": [],
        "staff_auth": {}, "staff_auth_history": [],
        "managed_groups": [], "specialties": [], "practice_settings": {},
        "custom_practices": [], "custom_sections": [], "requests": [],
        "processed_updates": [], "last_user_update_ts": {},
    }


def load_data() -> dict[str, Any]:
    """Загружает JSON, дополняет отсутствующие разделы, не удаляя пользовательские поля."""
    with DATA_LOCK:
        if not DATA_FILE.exists():
            return _empty_data()
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"bot_data.json повреждён или не читается: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Корень bot_data.json должен быть JSON-объектом; исходный файл не перезаписан.")

        defaults = _empty_data()
        for key, default in defaults.items():
            if key not in data or not isinstance(data[key], type(default)):
                # Старый access_codes иногда бывает словарём — преобразуем без потери записей.
                if key == "access_codes" and isinstance(data.get(key), dict):
                    converted = []
                    for code_key, value in data[key].items():
                        if isinstance(value, dict):
                            converted.append({"code": str(value.get("code", code_key)), **value})
                        else:
                            converted.append({"code": str(code_key), "role": str(value), "is_active": True})
                    data[key] = converted
                elif key == "links" and isinstance(data.get(key), dict):
                    data[key] = list(data[key].values())
                else:
                    data[key] = default.copy() if isinstance(default, (dict, list)) else default

        # Приводим связи к общей форме и одновременно поддерживаем схему JSON:
        # student_id — идентификатор студента; старый код проекта — student_code.
        data["links"] = [x for x in data["links"] if isinstance(x, dict)]
        for link in data["links"]:
            identifier = link.get("student_code", link.get("student_id"))
            resolved = resolve_student_code(identifier, data) if identifier is not None else None
            if resolved:
                link["student_code"] = resolved
                link.setdefault("student_id", data["students"].get(resolved, {}).get("id", resolved))
            link.setdefault("created_at", int(time.time()))
            link.setdefault("id", "LK-" + secrets.token_hex(4).upper())
        if not isinstance(data["access_codes"], list):
            data["access_codes"] = []
        data["access_codes"] = [x for x in data["access_codes"] if isinstance(x, dict)]

        # Миграция прежней модели users -> таблицы ролей / links.
        for uid, user in list(data["users"].items()):
            if not isinstance(user, dict):
                continue
            role = user.get("role")
            if role in ("teacher", "employer", "admin"):
                table = data[role + "s"]
                table.setdefault(str(uid), {
                    "max_user_id": str(uid), "fio": user.get("full_name", ""),
                    "position": user.get("position", ""),
                    "created_at": user.get("created_at", int(time.time())),
                })
                for code in (user.get("linked_student_codes", []) or []):
                    if isinstance(code, str):
                        _append_link(data, role, uid, code)
                legacy_code = user.get("linked_student_code")
                if legacy_code:
                    _append_link(data, role, uid, str(legacy_code))

        # Старые данные одного отчёта/дневника переносим в PM_01.
        for code, old in list(data["practice_reports"].items()):
            if isinstance(old, dict) and "sections" in old and not any(p in old for p in PRACTICES):
                data["practice_reports"][code] = {"PM_01": old}
        for code, old in list(data["practice_diary"].items()):
            if isinstance(old, list):
                data["practice_diary"][code] = {"PM_01": old}

        if security_manager.migrate_access_codes(data):
            # Старые открытые коды переводятся на хэши и больше не хранятся в JSON.
            save_data(data)
        return data


def save_data(data: dict[str, Any]) -> None:
    """Атомарная запись, чтобы сбой во время записи не обнулил bot_data.json."""
    with DATA_LOCK:
        temp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(DATA_FILE)


def _link_student_code(row: dict, data: dict) -> str | None:
    """Читает как внутреннее поле student_code, так и student_id из схемы JSON."""
    identifier = row.get("student_code", row.get("student_id"))
    return resolve_student_code(identifier, data) if identifier is not None else None


def _append_link(data: dict[str, Any], role: str, uid: Any, code: str) -> None:
    student = data["students"].get(str(code), {})
    row = {
        "id": "LK-" + secrets.token_hex(4).upper(),
        "student_id": student.get("id", str(code)),
        "student_code": str(code),  # внутреннее имя для совместимости с прежней версией
        "staff_type": role,
        "staff_id": str(uid),
        "created_at": int(time.time()),
    }
    if not any(x.get("staff_type") == role and str(x.get("staff_id")) == str(uid)
               and _link_student_code(x, data) == str(code) for x in data["links"]):
        data["links"].append(row)


def _http_error(prefix: str, response: requests.Response) -> None:
    # Токены и заголовки авторизации никогда не печатаются.
    print(f"{prefix}: HTTP {response.status_code}: {response.text[:500]}")


def send_message(uid: Any, text: str, buttons: list | None = None,
                 attachments: list[dict] | None = None,
                 text_format: str | None = None) -> bool:
    if uid is None:
        return False
    body: dict[str, Any] = {"text": str(text)[:4000]}
    if text_format in {"html", "markdown"}:
        # MAX requires an explicit format flag; otherwise tags/markup are sent literally.
        body["format"] = text_format
    outgoing = list(attachments or [])
    if buttons:
        outgoing.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
    if outgoing:
        body["attachments"] = outgoing
    try:
        _throttle_user(uid)
        response = HTTP.post(
            f"{API_URL}/messages", params={"user_id": int(uid)},
            headers=HEADERS, json=body, timeout=(1.5, 3.5),
        )
        if response.status_code not in (200, 201):
            _http_error("MAX send", response)
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        print(f"MAX send connection: {exc}")
        return False


def send_file_attachment(uid: Any, file_info: dict, context: str = "") -> bool:
    """Повторно отправляет сотруднику вложение по токену, полученному от MAX.

    MAX возвращает token и url прямо в объекте входящего вложения. При наличии
    token повторно используем его в POST /messages; если остался только url,
    отправляем ссылку как запасной вариант.
    """
    if uid is None or not isinstance(file_info, dict):
        return False
    token = file_info.get("token")
    url = file_info.get("url")
    filename = str(file_info.get("name") or "документ")
    label = f"📎 {filename}"
    if context:
        label += f" — {context}"
    if token:
        kind = str(file_info.get("type") or "file").lower()
        if kind not in {"file", "image", "video", "audio"}:
            kind = "file"
        body = {
            "text": label,
            "attachments": [{"type": kind, "payload": {"token": str(token)}}],
        }
        try:
            _throttle_user(uid)
            response = HTTP.post(
                f"{API_URL}/messages", params={"user_id": int(uid)},
                headers=HEADERS, json=body, timeout=(2, 8),
            )
            if response.status_code in (200, 201):
                return True
            _http_error("MAX resend attachment", response)
        except (requests.RequestException, ValueError) as exc:
            print(f"MAX resend attachment connection: {type(exc).__name__}")
    if url:
        return send_message(uid, f"{label}\nСсылка на файл: {url}")
    send_message(uid, f"⚠️ Не удалось передать {filename}: MAX не вернул токен или ссылку файла.")
    return False


def answer_callback(callback_id: str | None) -> None:
    if not callback_id:
        return
    try:
        response = requests.post(
            f"{API_URL}/answers", params={"callback_id": callback_id},
            headers=HEADERS, json={"notification": "Принято"}, timeout=(1, 2),
        )
        if response.status_code not in (200, 201):
            _http_error("MAX callback answer", response)
    except requests.RequestException as exc:
        print(f"Callback answer connection: {exc}")


def flow_screen(uid: Any, flow: dict, key: str, replacements: dict | None = None,
                extra: list | None = None) -> None:
    screen = flow.get(key) if isinstance(flow, dict) else None
    if not isinstance(screen, dict):
        send_message(uid, f"Экран «{key}» отсутствует в {FLOW_FILE.name}.")
        return
    message = str(screen.get("message", ""))
    for name, value in (replacements or {}).items():
        safe_value = html.escape(str(value), quote=False)
        message = message.replace("{" + str(name) + "}", safe_value)
    buttons = []
    for row in screen.get("keyboard", []) or []:
        converted = []
        for item in row:
            if not isinstance(item, dict):
                continue
            payload = item.get("callback_data", item.get("payload", ""))
            if payload:
                converted.append({"type": "callback", "text": str(item.get("text", "Кнопка")), "payload": str(payload)})
        if converted:
            buttons.append(converted)
    if extra:
        buttons.extend(extra)
    send_message(uid, message or "Выберите действие:", buttons or None, text_format="html")


def role_of(uid: Any, data: dict) -> str | None:
    key = str(uid)
    role = data.get("users", {}).get(key, {}).get("role")
    if role in ("student", "teacher", "employer", "admin"):
        return role
    for role in ("teacher", "employer", "admin"):
        if key in data.get(role + "s", {}):
            return role
    for code, student in data.get("students", {}).items():
        if isinstance(student, dict) and str(student.get("max_user_id", student.get("user_id", ""))) == key:
            return "student"
    return None



def role_label(role: str | None) -> str:
    return {"student": "студента", "teacher": "преподавателя", "employer": "работодателя", "admin": "администратора"}.get(role, "не выбрана")


def available_groups(data: dict) -> list[str]:
    """Configured groups plus active groups created in the management menu."""
    names = list(GROUPS)
    for item in data.get("managed_groups", []):
        if isinstance(item, dict) and item.get("active", True):
            name = str(item.get("name", "")).strip()
            if name and name.casefold() not in {x.casefold() for x in names}:
                names.append(name)
    return names


def group_by_id(data: dict, group_id: str) -> dict | None:
    return next((x for x in data.get("managed_groups", [])
                 if isinstance(x, dict) and str(x.get("id")) == str(group_id)), None)


def can_manage_group(uid: Any, role: str, group: dict) -> bool:
    return role == "admin" or (role == "teacher" and str(group.get("created_by")) == str(uid))


def practice_settings(data: dict, practice: str) -> dict:
    settings = data.setdefault("practice_settings", {}).setdefault(practice, {})
    settings.setdefault("title", "")
    settings.setdefault("description", "")
    settings.setdefault("practice_type", "не указан")
    return settings


def practice_label(practice: str, data: dict | None = None) -> str:
    base = display_practice(practice)
    if data is None or practice not in PRACTICES:
        return base
    title = str(data.get("practice_settings", {}).get(practice, {}).get("title") or "").strip()
    return f"{base} — {title}" if title else base


def custom_sections_for_group(data: dict, group_name: str, practice: str) -> list[dict]:
    # Возвращаем и архивные записи: иначе преподаватель не сможет восстановить раздел.
    # Эти записи пока не подключены к студенческому REPORT_MENU.
    return [x for x in data.get("custom_sections", [])
            if isinstance(x, dict) and x.get("group_name") == group_name
            and x.get("pm_code") == practice]


def _clean_short_text(value: str, limit: int = 60) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _group_manager_menu(uid: Any, data: dict) -> None:
    role = role_of(uid, data)
    rows = [[{"type": "callback", "text": "➕ Добавить группу", "payload": "GROUP_CREATE"}]]
    managed = [x for x in data.get("managed_groups", []) if isinstance(x, dict)
               and (role == "admin" or str(x.get("created_by")) == str(uid))]
    for group in managed:
        state_label = "активна" if group.get("active", True) else "архив"
        rows.append(_button(f"{group.get('name', 'Без названия')} — {state_label}", f"GROUP_ITEM:{group.get('id')}"))
    rows.append(_button("➕ Добавить специальность", "SPECIALTY_CREATE"))
    rows.append(_button("📚 Специальности", "SPECIALTY_LIST"))
    rows.append(_button("Назад", "TEACHER_MENU" if role == "teacher" else "ADMIN_MENU"))
    send_message(uid, "Управление группами. Группы из .env доступны для регистрации, но здесь не редактируются. Созданные здесь группы можно переименовывать и архивировать.", rows)


def _practice_manager_menu(uid: Any, data: dict) -> None:
    rows = []
    for practice in PRACTICES:
        meta = data.get("practice_settings", {}).get(practice, {})
        title = meta.get("title") or "без названия"
        rows.append(_button(f"{display_practice(practice)} — {title}", f"PRACTICE_ITEM:{practice}"))
    rows.append(_button("➕ Добавить практику (каталог)", "CUSTOM_PRACTICE_CREATE"))
    for item in data.get("custom_practices", []):
        if isinstance(item, dict):
            state_label = "архив" if not item.get("active", True) else "не подключена к маршруту"
            rows.append(_button(f"{item.get('title', 'Новая практика')} — {state_label}", f"CUSTOM_PRACTICE_ITEM:{item.get('id')}"))
    rows.append(_button("Назад", "TEACHER_MENU" if role_of(uid, data) == "teacher" else "ADMIN_MENU"))
    send_message(uid, "Настройка практик. Восемь базовых кодов и их последовательность защищены: можно изменить название, описание и тип. Новые записи пока сохраняются в каталоге, но не включаются в маршрут сдачи.", rows)


def _send_generated_file(uid: Any, filename: str, content: bytes, caption: str) -> bool:
    """Upload a generated file via MAX /uploads and send it as a file attachment."""
    if not BOT_TOKEN:
        send_message(uid, "Не настроен токен MAX — файл сформирован, но отправить его нельзя.")
        return False
    try:
        upload_response = HTTP.post(f"{API_URL}/uploads", params={"type": "file"},
                                    headers=HEADERS, timeout=(3, 15))
        if upload_response.status_code != 200:
            _http_error("MAX upload init", upload_response)
            send_message(uid, "MAX не выдал ссылку для загрузки файла. Попробуйте позже.")
            return False
        upload_info = upload_response.json()
        upload_url = upload_info.get("url")
        token = upload_info.get("token")
        if not upload_url:
            send_message(uid, "MAX не вернул адрес загрузки файла.")
            return False
        # Сначала всегда передаём сами байты файла, даже если endpoint уже вернул token.
        # В некоторых ответах /uploads token необязателен и появляется после загрузки.
        initial_token = token
        upload_response = HTTP.post(
            upload_url,
            files={"data": (filename, content, "application/octet-stream")},
            timeout=(5, 60),
        )
        if upload_response.status_code not in (200, 201):
            _http_error("MAX upload file", upload_response)
            send_message(uid, "Не удалось загрузить файл в MAX.")
            return False
        token = (upload_response.json().get("token") if upload_response.content else None) or initial_token
        if not token:
            send_message(uid, "MAX не вернул токен загруженного файла.")
            return False
        body = {"text": caption[:1000], "attachments": [{"type": "file", "payload": {"token": token}}]}
        # MAX обрабатывает загруженные файлы асинхронно. Если сообщение отклонено
        # именно из-за ещё не готового вложения, повторяем отправку с увеличением паузы.
        for attempt, delay in enumerate((1.0, 2.0, 4.0, 0.0)):
            if delay:
                time.sleep(delay)
            response = HTTP.post(f"{API_URL}/messages", params={"user_id": int(uid)},
                                 headers=HEADERS, json=body, timeout=(3, 20))
            if response.status_code in (200, 201):
                return True
            try:
                error_payload = response.json()
                error_text = json.dumps(error_payload, ensure_ascii=False).casefold()
            except (ValueError, TypeError):
                error_text = (getattr(response, "text", "") or "").casefold()
            not_ready = "attachment.not.ready" in error_text or "attachment not ready" in error_text
            if not_ready and attempt < 3:
                continue
            _http_error("MAX send generated file", response)
            send_message(uid, "Файл загружен, но MAX не отправил его в чат. Попробуйте выгрузить ещё раз.")
            return False
        return False
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"Generated file delivery failed: {type(exc).__name__}")
        send_message(uid, "Не удалось передать файл через MAX. Попробуйте позже.")
        return False


def _report_text_for_export(code: str, practice: str, data: dict) -> str:
    student = data.get("students", {}).get(code, {})
    report = report_for(data, code, practice)
    lines = ["ПРАКТИКБОТ — ВЫГРУЗКА РАБОТЫ", "",
             f"ФИО: {student.get('full_name', student.get('fio', '—'))}",
             f"Группа: {student.get('group_name', '—')}",
             f"Практика: {practice_label(practice, data)}",
             f"Статус: {status_label(report.get('status'))}",
             f"Оценка: {report.get('teacher_grade') or 'не выставлена'}", ""]
    if practice in REPORT_PRACTICES:
        for number in range(1, 5):
            value = str(report.get("sections", {}).get(str(number), "") or "").strip()
            files = report.get("files", {}).get(str(number), [])
            if isinstance(files, dict):
                files = [files]
            names = ", ".join(str(x.get("name") or "файл") for x in files if isinstance(x, dict))
            lines.extend([f"РАЗДЕЛ {number}", value or (f"Вложение: {names}" if names else "Не заполнен"), ""])
    else:
        lines.extend(["ЗАПИСИ ДНЕВНИКА", ""])
        for item in diary_for(data, code, practice):
            lines.append(f"{item.get('entry_date', '—')} | {item.get('pc_code') or 'ПК не определена'}")
            lines.append(str(item.get("comment", "")))
            lines.append("")
    if report.get("teacher_comment"):
        lines.extend(["КОММЕНТАРИЙ ПРЕПОДАВАТЕЛЯ", str(report["teacher_comment"]), ""])
    if report.get("employer_note"):
        lines.extend(["КОММЕНТАРИЙ НАСТАВНИКА", str(report["employer_note"]), ""])
    return "\n".join(lines)


def _export_admin_xlsx(uid: Any, data: dict) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        wb = Workbook()
        ws = wb.active
        ws.title = "Сводка"
        headers = ["ФИО", "Группа", "Код студента", "Практика", "Тип", "Статус", "Оценка", "Записей дневника"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="244062")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for code, student in data.get("students", {}).items():
            if not isinstance(student, dict):
                continue
            for practice in PRACTICES:
                report = report_for(data, code, practice)
                ws.append([student.get("full_name", student.get("fio", "—")),
                           student.get("group_name", "—"), code,
                           practice_label(practice, data),
                           "Отчёт" if practice in REPORT_PRACTICES else "Дневник",
                           status_label(report.get("status")), report.get("teacher_grade") or "—",
                           len(diary_for(data, code, practice)) if practice in DIARY_PRACTICES else 0])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = {"A": 30, "B": 16, "C": 18, "D": 28, "E": 14, "F": 18, "G": 14, "H": 20}
        for col, width in widths.items():
            ws.column_dimensions[col].width = width
        buffer = io.BytesIO()
        wb.save(buffer)
        filename = f"praktikbot_summary_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        _send_generated_file(uid, filename, buffer.getvalue(), "Сводный отчёт по студентам и практикам.")
    except Exception as exc:
        print(f"XLSX export failed: {type(exc).__name__}")
        send_message(uid, "Не удалось сформировать Excel. Проверьте установку openpyxl.")


def display_practice(practice: str) -> str:
    return str(practice).replace("PM_", "ПМ.")


def status_label(status: Any) -> str:
    return {"draft": "Заполняется", "submitted": "На проверке", "graded": "Оценено"}.get(str(status or "draft"), "Заполняется")


def resolve_student_code(identifier: Any, data: dict) -> str | None:
    value = str(identifier or "")
    if value in data.get("students", {}):
        return value
    for code, student in data.get("students", {}).items():
        if not isinstance(student, dict):
            continue
        if value in {str(student.get("id", "")), str(student.get("max_user_id", "")), str(student.get("invite_code", ""))}:
            return str(code)
    return None


def feedback_lines(report: dict) -> list[str]:
    lines = []
    grade = report.get("teacher_grade")
    if grade:
        lines.append(f"Оценка преподавателя: {grade}")
    teacher_comment = str(report.get("teacher_comment") or "").strip()
    if teacher_comment:
        lines.append("Комментарий преподавателя:\n" + teacher_comment)
    if report.get("employer_approved"):
        lines.append("Подтверждение наставника: получено")
    employer_note = str(report.get("employer_note") or "").strip()
    if employer_note:
        lines.append("Комментарий наставника:\n" + employer_note)
    return lines


def student_code(uid: Any, data: dict) -> str | None:
    key = str(uid)
    user = data.get("users", {}).get(key, {})
    code = user.get("invite_code")
    if code and code in data.get("students", {}):
        return code
    for code, student in data.get("students", {}).items():
        if isinstance(student, dict) and str(student.get("max_user_id", student.get("user_id", ""))) == key:
            return code
    return None


def report_for(data: dict, code: str, practice: str) -> dict:
    by_practice = data["practice_reports"].setdefault(code, {})
    if isinstance(by_practice, list):
        by_practice = {"PM_01": {"diary_legacy": by_practice}}
        data["practice_reports"][code] = by_practice
    report = by_practice.setdefault(practice, {})
    report.setdefault("sections", {})
    report.setdefault("files", {})
    report.setdefault("status", "draft")
    report.setdefault("teacher_grade", None)
    if report.get("teacher_grade") and report.get("status") == "draft":
        report["status"] = "graded"
    report.setdefault("employer_approved", False)
    report.setdefault("teacher_comment", "")
    report.setdefault("employer_note", "")
    # Совместимость с полями из схемы JSON: section_N_text/file_id.
    for n in range(1, 5):
        flat_text = report.get(f"section_{n}_text")
        flat_file = report.get(f"section_{n}_file_id")
        if flat_text and not report["sections"].get(str(n)):
            report["sections"][str(n)] = str(flat_text)
        if flat_file and not report["files"].get(str(n)):
            report["files"][str(n)] = [{
                "type": "file", "token": str(flat_file),
                "url": report.get(f"section_{n}_file_url"),
                "name": report.get(f"section_{n}_file_name"),
            }]
    return report


def diary_for(data: dict, code: str, practice: str) -> list:
    by_practice = data["practice_diary"].setdefault(code, {})
    if isinstance(by_practice, list):
        by_practice = {"PM_01": by_practice}
        data["practice_diary"][code] = by_practice
    entries = by_practice.setdefault(practice, [])
    if not isinstance(entries, list):
        entries = []
        by_practice[practice] = entries
    return entries


def practice_is_open(data: dict, code: str, practice: str) -> bool:
    if practice not in PRACTICES or not code or code not in data["students"]:
        return False
    if practice == "PM_01":
        return True
    previous = PRACTICES[PRACTICES.index(practice) - 1]
    return bool(report_for(data, code, previous).get("teacher_grade"))


def is_linked(uid: Any, code: str, data: dict, role: str | None = None) -> bool:
    role = role or role_of(uid, data)
    return any(
        row.get("staff_type") == role
        and str(row.get("staff_id")) == str(uid)
        and _link_student_code(row, data) == code
        for row in data["links"]
    )


def add_link(uid: Any, role: str, code: str, data: dict) -> bool:
    if role not in ("teacher", "employer", "admin") or code not in data["students"]:
        return False
    _append_link(data, role, uid, code)
    user = data["users"].setdefault(str(uid), {"role": role})
    linked = user.setdefault("linked_student_codes", [])
    if code not in linked:
        linked.append(code)
    return True


def _button(text: str, payload: str) -> list:
    return [{"type": "callback", "text": text, "payload": payload}]


def _staff_auth_bypassed() -> bool:
    """Только для осознанно включённого тестового режима или отключённой защиты."""
    return (not SECURITY_ENABLED) or TESTING_AUTH_MODE


def _staff_is_authorized(uid: Any, role: str, data: dict) -> bool:
    if role not in STAFF_ROLES:
        return False
    if _staff_auth_bypassed():
        return True
    return security_manager.is_staff_authorized(uid, role, data)


def begin_staff_auth(uid: Any, role: str, data: dict) -> None:
    """Запрашивает одноразовый код, не назначая роль до его проверки."""
    if role not in STAFF_ROLES:
        send_message(uid, "Неизвестная роль.")
        return
    key = str(uid)
    data.setdefault("temporary", {}).setdefault(key, {})["requested_role"] = role
    data.setdefault("states", {})[key] = "staff_access_code"
    save_data(data)
    role_text = {"teacher": "преподавателя", "employer": "работодателя/наставника", "admin": "администратора"}[role]
    send_message(
        uid,
        f"Для регистрации как {role_text} введите одноразовый код доступа.\n"
        "Код действует 24 часа с момента выпуска и может быть использован только один раз."
    )


def _finish_staff_registration(uid: Any, role: str, data: dict) -> None:
    """Создаёт профиль сотрудника только после успешной проверки кода."""
    key = str(uid)
    user = data["users"].setdefault(key, {})
    user["role"] = role
    profile = data[role + "s"].setdefault(key, {
        "max_user_id": key,
        "fio": user.get("full_name", ""),
        "created_at": int(time.time()),
    })
    profile["max_user_id"] = key
    fio = (user.get("full_name") or profile.get("fio") or "").strip()
    position = (user.get("position") or profile.get("position") or "").strip()
    data["states"].pop(key, None)
    data["temporary"].pop(key, None)
    if not fio:
        data["states"][key] = "staff_fio"
        save_data(data)
        send_message(uid, "Код принят. Введите ФИО полностью (фамилия, имя, отчество).")
        return
    if role == "employer" and not position:
        data["states"][key] = "employer_position"
        save_data(data)
        send_message(uid, "Код принят. Введите вашу должность (например: наставник, мастер, инженер).")
        return
    save_data(data)
    staff_menu(uid, role)


def start(uid: Any, data: dict | None = None) -> None:
    data = data if data is not None else load_data()
    role = role_of(uid, data)
    key = str(uid)
    if role == "student":
        code = student_code(uid, data)
        if code:
            student_menu(uid, data)
        else:
            register_student(uid, data)
        return
    if role in STAFF_ROLES:
        if not _staff_is_authorized(uid, role, data):
            begin_staff_auth(uid, role, data)
            return
        profile = data[role + "s"].setdefault(key, {"max_user_id": key})
        fio = (data["users"].get(key, {}).get("full_name") or profile.get("fio") or "").strip()
        if not fio:
            data["states"][key] = "staff_fio"
            save_data(data)
            send_message(uid, "Для завершения регистрации введите ФИО полностью (3 слова).")
            return
        if role == "employer" and not (data["users"].get(key, {}).get("position") or profile.get("position")):
            data["states"][key] = "employer_position"
            save_data(data)
            send_message(uid, "Введите вашу должность.")
            return
        staff_menu(uid, role)
        return
    rows = [
        _button("🎓 Я студент", "ROLE:student"),
        _button("📚 Я преподаватель", "ROLE:teacher"),
        _button("🏭 Я работодатель", "ROLE:employer"),
        _button("🏢 Я администратор", "ROLE:admin"),
    ]
    send_message(uid, "Здравствуйте! Выберите свою роль:", rows)


def student_menu(uid: Any, data: dict, notice: str | None = None) -> None:
    code = student_code(uid, data)
    student = data["students"].get(code) if code else None
    if not isinstance(student, dict):
        send_message(uid, "Профиль студента не найден. Напишите /start.")
        return
    graded = sum(bool(report_for(data, code, p).get("teacher_grade")) for p in PRACTICES)
    rows = []
    for practice in PRACTICES:
        report = report_for(data, code, practice)
        opened = practice_is_open(data, code, practice)
        if report.get("teacher_grade"):
            icon, status = "✅", f"Сдано: {report['teacher_grade']}"
        elif report.get("status") == "submitted":
            icon, status = "⏳", "На проверке"
        elif opened:
            icon, status = "📝", "Доступно"
        else:
            icon, status = "🔒", "Закрыто"
        rows.append(_button(f"{icon} {practice_label(practice, data)} — {status}", f"PRACTICE:{practice}"))
    rows.extend([
        _button("📖 Дневники практики", "DIARY_MENU"),
        _button("🤖 Спросить ИИ-помощника", "ASK_AI"),
        _button("Показать мой код", "MY_CODE"),
    ])
    code_line = f"\nКод для привязки: {code}"
    header = (notice + "\n\n") if notice else ""
    send_message(
        uid,
        header + f"Студент: {student.get('full_name', student.get('fio', '—'))}\n"
        f"Группа: {student.get('group_name', '—')}\nПройдено: {graded}/8"
        + code_line + "\nВыберите практику:",
        rows,
    )


def staff_menu(uid: Any, role: str) -> None:
    flows = {"teacher": TEACHER_FLOW, "employer": EMPLOYER_FLOW, "admin": ADMIN_FLOW}
    keys = {"teacher": "TEACHER_MENU", "employer": "EMPLOYER_MENU", "admin": "ADMIN_MENU"}
    flow, key = flows[role], keys[role]

    if role == "admin":
        # Администрации доступны только четыре согласованных раздела.
        if isinstance(flow, dict) and isinstance(flow.get(key), dict) and flow[key].get("keyboard"):
            flow_screen(uid, flow, key, extra=[
                _button("🤖 ИИ-помощник", "ASK_AI"),
                _button("🚪 Выйти из профиля", "LOGOUT"),
            ])
        else:
            send_message(
                uid,
                "🏢 <b>Панель администратора</b>\n\nВыберите действие:",
                [
                    _button("📥 Экспорт в Excel", "ADMIN_EXPORT_XLSX"),
                    _button("👥 Управление группами", "GROUP_MGMT"),
                    _button("📊 Полная статистика", "ADMIN_STATS"),
                    _button("📨 Заявки студентов", "STAFF_REQUESTS"),
                    _button("🚪 Выйти из профиля", "LOGOUT"),
                ],
                text_format="html",
            )
        return

    if isinstance(flow, dict) and isinstance(flow.get(key), dict) and flow[key].get("keyboard"):
        extras = [_button("🤖 ИИ-помощник", "ASK_AI")]
        if role == "teacher":
            extras.extend([
                _button("👥 Управление группами", "GROUP_MGMT"),
                _button("📚 Управление практиками", "PRACTICE_MGMT"),
                _button("🧩 Настройка доп. разделов отчёта", "CUSTOM_SECTIONS_MGMT"),
                _button("📨 Заявки студентов", "STAFF_REQUESTS"),
                _button("🔔 Напомнить о дневнике", "REMINDER_MENU"),
            ])
        extras.append(_button("🚪 Выйти из профиля", "LOGOUT"))
        flow_screen(uid, flow, key, extra=extras)
        return

    fallback = {
        "teacher": [_button("➕ Добавить студента", "TEACHER_ADD_STUDENT"),
                    _button("📋 Мои студенты", "TEACHER_STUDENT_LIST"),
                    _button("⚠️ Требуют проверки", "TEACHER_NEED_CHECK"),
                    _button("✅ Проверенные", "TEACHER_ALREADY_CHECKED"),
                    _button("👥 Управление группами", "GROUP_MGMT"),
                    _button("📚 Управление практиками", "PRACTICE_MGMT"),
                    _button("🧩 Настройка доп. разделов отчёта", "CUSTOM_SECTIONS_MGMT"),
                    _button("📨 Заявки студентов", "STAFF_REQUESTS"),
                    _button("🔔 Напомнить о дневнике", "REMINDER_MENU"),
                    _button("🤖 ИИ-помощник", "ASK_AI"),
                    _button("🚪 Выйти из профиля", "LOGOUT")],
        "employer": [_button("➕ Добавить студента", "EMPLOYER_ADD_STUDENT"),
                     _button("👨‍🎓 Мои студенты", "EMPLOYER_STUDENT_LIST"),
                     _button("🤖 ИИ-помощник", "ASK_AI"),
                     _button("🚪 Выйти из профиля", "LOGOUT")],
        "admin": [_button("📥 Экспорт в Excel", "ADMIN_EXPORT_XLSX"),
                  _button("👥 Управление группами", "GROUP_MGMT"),
                  _button("📊 Полная статистика", "ADMIN_STATS"),
                  _button("📨 Заявки студентов", "STAFF_REQUESTS"),
                  _button("🤖 ИИ-помощник", "ASK_AI"),
                  _button("🚪 Выйти из профиля", "LOGOUT")],
    }
    send_message(uid, "Главное меню:", fallback[role])


def role_auth(uid: Any, role: str, data: dict) -> None:
    """Выбор роли сотрудника; при включённой защите нужен личный одноразовый код."""
    if role not in STAFF_ROLES:
        send_message(uid, "Неизвестная роль.")
        return
    existing = role_of(uid, data)
    if existing and existing != role:
        send_message(uid, "За аккаунтом MAX уже закреплена другая роль. Изменить её можно только после выхода из профиля.")
        return
    key = str(uid)
    if _staff_auth_bypassed():
        user = data["users"].setdefault(key, {})
        user["role"] = role
        profile = data[role + "s"].setdefault(key, {
            "max_user_id": key, "fio": user.get("full_name", ""),
            "created_at": int(time.time()),
        })
        profile["max_user_id"] = key
        if existing == role:
            fio = user.get("full_name") or profile.get("fio")
            position = user.get("position") or profile.get("position")
            if fio and (role != "employer" or position):
                data["states"].pop(key, None)
                save_data(data)
                staff_menu(uid, role)
                return
        data["states"][key] = "staff_fio"
        save_data(data)
        send_message(uid, "Тестовый режим: введите ФИО полностью (фамилия, имя, отчество).")
        return

    if existing == role and _staff_is_authorized(uid, role, data):
        profile = data[role + "s"].get(key, {})
        fio = data["users"].get(key, {}).get("full_name") or profile.get("fio")
        position = data["users"].get(key, {}).get("position") or profile.get("position")
        if not fio:
            data["states"][key] = "staff_fio"
            save_data(data)
            send_message(uid, "Введите ФИО полностью (фамилия, имя, отчество).")
            return
        if role == "employer" and not position:
            data["states"][key] = "employer_position"
            save_data(data)
            send_message(uid, "Введите вашу должность.")
            return
        data["states"].pop(key, None)
        save_data(data)
        staff_menu(uid, role)
        return
    begin_staff_auth(uid, role, data)


def logout_staff(uid: Any, data: dict) -> None:
    """Выход отзывает профиль сотрудника и все его связи; код повторно не используется."""
    key = str(uid)
    role = role_of(uid, data)
    if role not in STAFF_ROLES:
        send_message(uid, "Активный профиль сотрудника не найден.")
        return
    security_manager.deactivate_staff_auth(data, key)
    data["users"].pop(key, None)
    data[role + "s"].pop(key, None)
    data["links"] = [
        row for row in data.get("links", [])
        if not (row.get("staff_type") == role and str(row.get("staff_id")) == key)
    ]
    data["states"].pop(key, None)
    data["temporary"].pop(key, None)
    save_data(data)
    if _staff_auth_bypassed():
        notice = "Тестовый профиль завершён. Повторный вход будет доступен в соответствии с текущим тестовым режимом."
    else:
        notice = "Вы вышли из профиля сотрудника. Для повторного входа, в том числе с этого аккаунта MAX, потребуется новый одноразовый код доступа."
    send_message(uid, notice, [_button("Войти снова", "START")])


def valid_fio(value: str) -> bool:
    value = " ".join((value or "").split())
    parts = value.split()
    return (
        len(value) <= 100 and len(parts) == 3
        and all(re.fullmatch(r"[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*", part) for part in parts)
    )


def normalize_fio(value: str) -> str:
    words = " ".join((value or "").split()).split()
    normalized = []
    for word in words:
        parts = word.split("-")
        normalized.append("-".join(part[:1].upper() + part[1:].lower() for part in parts))
    return " ".join(normalized)


def register_student(uid: Any, data: dict) -> None:
    key = str(uid)
    data["states"][key] = "student_group"
    save_data(data)
    rows = [_button(group, "GROUP:" + group) for group in available_groups(data)]
    rows.append(_button("Ввести другую группу", "GROUP_OTHER"))
    send_message(uid, "Выберите учебную группу:", rows)


def _section_hint(practice: str, number: str) -> str:
    """Ищет описание раздела в report_sections, practice_catalog или старых экранах."""
    backend = FLOW.get("backend_logic_requirements", {}) if isinstance(FLOW.get("backend_logic_requirements", {}), dict) else {}
    section_roots = FLOW.get("report_sections", backend.get("report_sections", {}))
    candidates: list[Any] = []
    if isinstance(section_roots, dict):
        practice_sections = section_roots.get(practice, section_roots)
        if isinstance(practice_sections, str) and practice_sections.startswith("use_same_as:"):
            alias = practice_sections.split(":", 1)[1].split()[0]
            practice_sections = section_roots.get(alias, {})
        if isinstance(practice_sections, dict):
            candidates.extend([practice_sections.get(number), practice_sections.get(f"section_{number}"),
                               practice_sections.get(f"SECTION_{number}")])
        elif isinstance(practice_sections, list):
            if int(number) <= len(practice_sections):
                candidates.append(practice_sections[int(number) - 1])
    catalog = FLOW.get("practice_catalog", backend.get("practice_catalog", {}))
    if isinstance(catalog, dict):
        item = catalog.get(practice, {})
        if isinstance(item, dict):
            sections = item.get("sections", item.get("report_sections", []))
            if isinstance(sections, dict):
                candidates.extend([sections.get(number), sections.get(f"section_{number}")])
            elif isinstance(sections, list) and int(number) <= len(sections):
                candidates.append(sections[int(number) - 1])
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            title = value.get("title") or value.get("name") or value.get("label") or value.get("description") or value.get("prompt")
            hint = value.get("hint") or value.get("section_hint") or ""
            if title and hint:
                return f"Раздел {number}. {str(title).strip()}\n\nПодсказка:\n{str(hint).strip()}"
            if title:
                return f"Раздел {number}. {str(title).strip()}"
            if hint:
                return str(hint).strip()
    # Совместимость со старым JSON: PM_01_SECTION_1..3.
    for key in (f"{practice}_SECTION_{number}", f"PM_01_SECTION_{number}"):
        item = STUDENT_FLOW.get(key)
        if isinstance(item, dict) and item.get("message"):
            return str(item["message"])
    defaults = {
        "1": "Раздел 1. Опишите предприятие и место прохождения практики.",
        "2": "Раздел 2. Опишите обязанности и выполняемые функции.",
        "3": "Раздел 3. Опишите соблюдение требований охраны труда.",
        "4": "Раздел 4. Подведите итоги и сформулируйте выводы по практике.",
    }
    return defaults.get(number, f"Раздел {number}. Опишите содержание раздела.")


def practice_screen(uid: Any, code: str, practice: str, data: dict) -> None:
    if practice not in PRACTICES:
        send_message(uid, "Неизвестная практика.")
        return
    if not practice_is_open(data, code, practice):
        previous = PRACTICES[PRACTICES.index(practice) - 1]
        send_message(uid, f"{display_practice(practice)} пока закрыта. Сначала преподаватель должен оценить {display_practice(previous)}.")
        return
    user = data["users"].setdefault(str(uid), {"role": "student"})
    user["current_practice"] = practice
    report = report_for(data, code, practice)
    if report.get("status") != "draft":
        editable = False
    else:
        editable = True
    if practice in REPORT_PRACTICES:
        rows = []
        for number in range(1, 5):
            n = str(number)
            filled = bool(report["sections"].get(n) or report["files"].get(n))
            rows.append(_button(f"Раздел {number}: {'✅ заполнен' if filled else 'не заполнен'}", f"SECTION:{practice}:{number}"))
        if editable:
            rows.append(_button("📨 Отправить отчёт", f"SUBMIT:{practice}"))
        rows.append(_button("Назад", "STUDENT_MENU"))
        header = [f"{practice_label(practice, data)} — отчёт", f"Состояние: {status_label(report.get('status'))}"]
        header.extend(feedback_lines(report))
        send_message(uid, "\n\n".join(header), rows)
    else:
        rows = [_button("➕ Добавить запись", f"DIARY_ADD:{practice}"),
                _button("📖 Просмотреть дневник", f"DIARY_VIEW:{practice}:0")]
        if editable:
            rows.append(_button("📨 Отправить дневник", f"SUBMIT:{practice}"))
        rows.append(_button("Назад", "STUDENT_MENU"))
        header = [f"{practice_label(practice, data)} — дневник", f"Записей: {len(diary_for(data, code, practice))}",
                  f"Состояние: {status_label(report.get('status'))}"]
        header.extend(feedback_lines(report))
        send_message(uid, "\n".join(header), rows)
    save_data(data)


def normalize_payload(uid: Any, payload: Any, data: dict) -> str:
    """Сводит команды старого JSON и новых кнопок к единому формату."""
    if isinstance(payload, dict):
        payload = payload.get("payload", payload.get("callback_data", ""))
    payload = str(payload or "").strip()
    role = role_of(uid, data)
    key = str(uid)
    aliases = {
        "ROLE_STUDENT": "ROLE:student", "ROLE_TEACHER": "ROLE:teacher",
        "ROLE_EMPLOYER": "ROLE:employer", "ROLE_ADMIN": "ROLE:admin",
        "START": "START", "AUTH_CONFIRM_YES": "REG_CONFIRM",
        "DIARY_CONFIRM_YES": "DIARY_SAVE", "DIARY_EDIT": "DIARY_EDIT",
        "AUTH_STUDENT_FIO": "AUTH_STUDENT_FIO", "AUTH_STUDENT_GROUP": "AUTH_STUDENT_GROUP",
        "STUDENT_MENU": "STUDENT_MENU", "SHOW_INVITE_CODE": "MY_CODE",
        "TEACHER_MENU": "TEACHER_MENU", "EMPLOYER_MENU": "EMPLOYER_MENU", "ADMIN_MENU": "ADMIN_MENU",
        "TEACHER_NEED_CHECK": "TEACHER_FILTER:submitted",
        "TEACHER_ALREADY_CHECKED": "TEACHER_FILTER:graded",
        "TEACHER_ADD_GRADE": "GRADE_SELECTED",
        "EMPLOYER_APPROVE_RECORD": "EMPLOYER_APPROVE_SELECTED",
        "EMPLOYER_ADD_NOTE": "EMPLOYER_NOTE_SELECTED",
        "PM_02_CHECK": "PRACTICE:PM_02",
        "PM_03_LOCKED": "PRACTICE:PM_03", "PM_04_LOCKED": "PRACTICE:PM_04",
        "PM_05_LOCKED": "PRACTICE:PM_05", "PM_06_LOCKED": "PRACTICE:PM_06",
        "PM_07_LOCKED": "PRACTICE:PM_07", "PM_08_LOCKED": "PRACTICE:PM_08",
    }
    if payload in aliases:
        return aliases[payload]
    match = re.fullmatch(r"(PM_0[1-8])_MENU", payload)
    if match:
        return "PRACTICE:" + match.group(1)
    match = re.fullmatch(r"(PM_0[1-8])_SECTION_([1-4])", payload)
    if match:
        return f"SECTION:{match.group(1)}:{match.group(2)}"
    if payload == "DIARY_ADD_DATE":
        return "DIARY_ADD:PM_03"
    if payload in ("DIARY_MENU", "DIARY_ADD_COMMENT", "DIARY_VIEW_ENTRIES"):
        return payload
    if payload.startswith("ACTION_SAVE_SECTION_"):
        number = payload.rsplit("_", 1)[-1]
        practice = data.get("users", {}).get(key, {}).get("current_practice", "PM_01")
        return f"SECTION_SAVE:{practice}:{number}"
    # Команды из текущего JSON приводятся к внутренним командам main.py.
    m = re.fullmatch(r"AUTH_GROUP:(.+)", payload)
    if m:
        return "GROUP:" + m.group(1)
    m = re.fullmatch(r"PRACTICE_OPEN:(PM_0[1-8])", payload)
    if m:
        return "PRACTICE:" + m.group(1)
    m = re.fullmatch(r"REPORT_SECTION:(PM_0[1-8]):([1-4])", payload)
    if m:
        return f"SECTION:{m.group(1)}:{m.group(2)}"
    m = re.fullmatch(r"REPORT_MENU:(PM_0[1-8])", payload)
    if m:
        return "PRACTICE:" + m.group(1)
    m = re.fullmatch(r"REPORT_INPUT:(PM_0[1-8]):([1-4])", payload)
    if m:
        return f"SECTION_SAVE:{m.group(1)}:{m.group(2)}"
    m = re.fullmatch(r"SUBMIT_REPORT:(PM_0[1-8])", payload) or re.fullmatch(r"SUBMIT_DIARY:(PM_0[1-8])", payload)
    if m:
        return "SUBMIT:" + m.group(1)
    m = re.fullmatch(r"DIARY_ADD_DATE:(PM_0[1-8])", payload)
    if m:
        return "DIARY_ADD:" + m.group(1)
    m = re.fullmatch(r"DIARY_VIEW_ENTRIES:(PM_0[1-8]):(\d+)", payload)
    if m:
        return f"DIARY_VIEW:{m.group(1)}:{max(0, int(m.group(2))-1)}"
    m = re.fullmatch(r"DIARY_PRACTICE_MENU:(PM_0[1-8])", payload)
    if m:
        return "PRACTICE:" + m.group(1)
    m = re.fullmatch(r"(TEACHER|EMPLOYER)_STUDENT_CARD:(.+)", payload)
    if m:
        code = resolve_student_code(m.group(2), data)
        return "VIEW:" + code if code else payload
    m = re.fullmatch(r"(TEACHER|EMPLOYER)_VIEW_PRACTICE:(.+):(PM_0[1-8])", payload)
    if m:
        code = resolve_student_code(m.group(2), data)
        return f"STAFF_PRACTICE:{code}:{m.group(3)}" if code else payload
    m = re.fullmatch(r"TEACHER_ADD_COMMENT:(.+):(PM_0[1-8])", payload)
    if m:
        code = resolve_student_code(m.group(1), data)
        return f"TEACHER_COMMENT:{code}:{m.group(2)}" if code else payload
    m = re.fullmatch(r"TEACHER_ADD_GRADE:(.+):(PM_0[1-8])", payload)
    if m:
        code = resolve_student_code(m.group(1), data)
        return f"GRADE:{code}:{m.group(2)}" if code else payload
    m = re.fullmatch(r"EMPLOYER_APPROVE_RECORD:(.+):(PM_0[1-8])", payload)
    if m:
        code = resolve_student_code(m.group(1), data)
        return f"APPROVE:{code}:{m.group(2)}" if code else payload
    m = re.fullmatch(r"EMPLOYER_ADD_NOTE:(.+):(PM_0[1-8])", payload)
    if m:
        code = resolve_student_code(m.group(1), data)
        return f"EMPLOYER_NOTE:{code}:{m.group(2)}" if code else payload
    if payload.startswith("EMPLOYER_VIEW_STUDENT_") or payload.startswith("TEACHER_VIEW_STUDENT_"):
        try:
            index = int(payload.rsplit("_", 1)[1]) - 1
            codes = [_link_student_code(x, data) for x in data["links"]
                     if x.get("staff_type") == role and str(x.get("staff_id")) == key
                     and _link_student_code(x, data) in data["students"]]
            if 0 <= index < len(codes):
                return "VIEW:" + str(codes[index])
        except (ValueError, TypeError):
            pass
    return payload


def _list_linked_codes(uid: Any, role: str, data: dict) -> list[str]:
    return list(dict.fromkeys(
        _link_student_code(x, data) for x in data["links"]
        if x.get("staff_type") == role and str(x.get("staff_id")) == str(uid)
        and _link_student_code(x, data) in data["students"]
    ))


def _render_diary_menu(uid: Any, code: str, data: dict) -> None:
    rows = []
    for practice in sorted(DIARY_PRACTICES):
        opened = practice_is_open(data, code, practice)
        report = report_for(data, code, practice)
        icon = "✅" if report.get("teacher_grade") else ("⏳" if report.get("status") == "submitted" else ("📝" if opened else "🔒"))
        rows.append(_button(f"{icon} {practice_label(practice, data)}", f"PRACTICE:{practice}"))
    rows.append(_button("Назад", "STUDENT_MENU"))
    send_message(uid, "Выберите дневник практики:", rows)


def _show_diary_page(uid: Any, code: str, practice: str, page: int, data: dict) -> None:
    if practice not in DIARY_PRACTICES or not practice_is_open(data, code, practice):
        send_message(uid, "Этот дневник пока недоступен.")
        return
    def entry_sort_key(item: dict) -> date:
        try:
            return datetime.strptime(str(item.get("entry_date", "")), "%d.%m.%Y").date()
        except ValueError:
            return date.min
    entries = sorted(diary_for(data, code, practice), key=entry_sort_key)
    start_idx = max(0, page) * 10
    chunk = entries[start_idx:start_idx + 10]
    text = "\n\n".join(
        f"{x.get('entry_date', '—')} — {x.get('pc_code') or 'ПК не определена'}\n{x.get('comment', '')}"
        for x in chunk
    )
    if not text:
        text = "Записей пока нет."
    rows = []
    if page > 0:
        rows.append(_button("← Назад", f"DIARY_VIEW:{practice}:{page-1}"))
    if start_idx + 10 < len(entries):
        rows.append(_button("Ещё →", f"DIARY_VIEW:{practice}:{page+1}"))
    rows.append(_button("Назад к практике", f"PRACTICE:{practice}"))
    send_message(uid, f"Дневник {practice_label(practice, data)} ({len(entries)} записей)\n\n{text}", rows)


def _render_student_practice(uid: Any, code: str, practice: str, data: dict) -> str:
    report = report_for(data, code, practice)
    student = data["students"][code]
    lines = [f"{student.get('full_name', student.get('fio', 'Студент'))} — {practice_label(practice, data)}",
             f"Состояние: {status_label(report.get('status'))}"]
    lines.extend(feedback_lines(report))
    if practice in REPORT_PRACTICES:
        for i in range(1, 5):
            value = report.get("sections", {}).get(str(i), "")
            file_info = report.get("files", {}).get(str(i))
            if isinstance(file_info, dict):
                file_items = [file_info]
            elif isinstance(file_info, list):
                file_items = file_info
            else:
                file_items = []
            file_names = ", ".join(str(x.get("name") or "файл") for x in file_items if isinstance(x, dict))
            content = value or (f"Документ прикреплён: {file_names or 'файл'}" if file_items else "не заполнен")
            lines.append(f"\nРаздел {i}:\n{content}")
    else:
        entries = diary_for(data, code, practice)
        lines.append(f"Записей дневника: {len(entries)}")
        for item in entries[-5:]:
            lines.append(f"\n{item.get('entry_date')} — {item.get('pc_code') or 'ПК не определена'}: {item.get('comment')}")
    return "\n".join(lines)


def _staff_student_view(uid: Any, code: str, role: str, data: dict, practice: str | None = None) -> None:
    if code not in data["students"] or not is_linked(uid, code, data, role):
        send_message(uid, "Нет доступа к этому студенту.")
        return
    user = data["users"].setdefault(str(uid), {"role": role})
    user["selected_student_code"] = code
    if practice is None:
        student = data["students"][code]
        rows = []
        for p in PRACTICES:
            r = report_for(data, code, p)
            rows.append(_button(f"{practice_label(p, data)} — {status_label(r.get('status'))} / {r.get('teacher_grade') or 'без оценки'}", f"STAFF_PRACTICE:{code}:{p}"))
        rows.append(_button("Назад", "TEACHER_MENU" if role == "teacher" else ("EMPLOYER_MENU" if role == "employer" else "ADMIN_MENU")))
        send_message(uid, f"Студент: {student.get('full_name', student.get('fio', code))}\nКод: {code}\nВыберите практику:", rows)
        return
    if practice not in PRACTICES:
        send_message(uid, "Неизвестная практика.")
        return
    user["current_practice"] = practice
    report = report_for(data, code, practice)
    lines = [_render_student_practice(uid, code, practice, data)]
    rows = []
    if role == "teacher" and report.get("status") == "submitted":
        rows.append(_button("✍️ Поставить оценку", f"GRADE:{code}:{practice}"))
    if role == "teacher" and report.get("status") in {"submitted", "graded"}:
        comment_label = "Изменить комментарий" if report.get("teacher_comment") else "Оставить комментарий"
        rows.append(_button("💬 " + comment_label, f"TEACHER_COMMENT:{code}:{practice}"))
    if role == "employer":
        rows.append(_button("✅ Подтвердить практику", f"APPROVE:{code}:{practice}"))
        rows.append(_button("📝 Оставить пометку", f"EMPLOYER_NOTE:{code}:{practice}"))
    if role in {"teacher", "admin"}:
        rows.append(_button("📥 Выгрузить работу (.txt)", f"EXPORT_REPORT:{code}:{practice}"))
    rows.append(_button("К списку практик", f"VIEW:{code}"))
    send_message(uid, lines[0], rows)
    if practice in REPORT_PRACTICES:
        for section_no in range(1, 5):
            section_files = report.get("files", {}).get(str(section_no), [])
            if isinstance(section_files, dict):
                section_files = [section_files]
            for file_info in section_files if isinstance(section_files, list) else []:
                if isinstance(file_info, dict):
                    send_file_attachment(uid, file_info, f"{display_practice(practice)}, раздел {section_no}")
    save_data(data)


def _admin_callback_is_allowed(payload: str) -> bool:
    """Оставляет администрации согласованные разделы и ИИ-помощника."""
    exact = {
        "ADMIN_MENU", "ADMIN_EXPORT_XLSX", "ADMIN_STATS", "GROUP_MGMT", "GROUP_CREATE", "ASK_AI",
        "SPECIALTY_CREATE", "SPECIALTY_LIST", "STAFF_REQUESTS",
    }
    prefixes = (
        "GROUP_ITEM:", "GROUP_RENAME:", "GROUP_TOGGLE:",
        "SPECIALTY_ITEM:", "SPECIALTY_RENAME:", "SPECIALTY_TOGGLE:",
        "REQUEST_VIEW:", "REQUEST_REPLY:",
    )
    return payload in exact or payload.startswith(prefixes)


def handle_callback(uid: Any, raw_payload: Any, data: dict) -> None:
    key = str(uid)
    payload = normalize_payload(uid, raw_payload, data)
    user = data["users"].setdefault(key, {})
    role = role_of(uid, data)

    if payload == "START":
        start(uid, data)
        return
    if payload == "LOGOUT":
        logout_staff(uid, data)
        return
    if role in STAFF_ROLES and not _staff_is_authorized(uid, role, data) and not payload.startswith("ROLE:"):
        begin_staff_auth(uid, role, data)
        return
    if role == "admin" and not _admin_callback_is_allowed(payload):
        send_message(uid, "Этот раздел недоступен для роли администратора.")
        return
    if payload == "AUTH_STUDENT_GROUP" and role in {None, "student"}:
        user["role"] = "student"
        data["states"][key] = "student_group"
        data["temporary"].pop(key, None)
        save_data(data)
        rows = [_button(g, "GROUP:" + g) for g in available_groups(data)]
        rows.append(_button("Ввести другую группу", "GROUP_OTHER"))
        send_message(uid, "Выберите учебную группу:", rows)
        return
    if payload == "AUTH_STUDENT_FIO" and role == "student":
        data["states"][key] = "student_fio"
        save_data(data)
        send_message(uid, "Введите ФИО полностью (фамилия, имя, отчество).")
        return

    if payload.startswith("ROLE:"):
        chosen = payload.split(":", 1)[1]
        if chosen == "student":
            if role and role != "student":
                send_message(uid, "За аккаунтом уже закреплена другая роль.")
                return
            user["role"] = "student"
            if student_code(uid, data):
                student_menu(uid, data)
            else:
                register_student(uid, data)
            return
        role_auth(uid, chosen, data)
        return

    if payload == "GROUP_OTHER" and role in {None, "student"}:
        user["role"] = "student"
        data["states"][key] = "student_group_custom"
        save_data(data)
        send_message(uid, "Введите название своей учебной группы (например, ЭЛС-23). До 30 символов.")
        return

    if payload.startswith("GROUP:") and role == "student" and data["states"].get(key) == "student_group":
        group = payload.split(":", 1)[1]
        if group not in available_groups(data):
            send_message(uid, "Неизвестная учебная группа. Выберите её из списка или введите вручную.")
            return
        data["temporary"][key] = {"group_name": group}
        data["states"][key] = "student_fio"
        save_data(data)
        send_message(uid, "Введите ФИО полностью (фамилия, имя, отчество).")
        return

    if payload == "REG_CONFIRM" and role == "student":
        temp = data["temporary"].get(key, {})
        group, fio = temp.get("group_name"), temp.get("fio")
        if not group or not valid_fio(fio):
            send_message(uid, "Данные регистрации устарели. Напишите /start.")
            return
        if any(x.get("group_name") == group and str(x.get("full_name", x.get("fio", ""))).casefold() == fio.casefold()
               for x in data["students"].values() if isinstance(x, dict)):
            send_message(uid, "Студент с таким ФИО уже зарегистрирован в этой группе.")
            return
        code = "ST-" + secrets.token_hex(3).upper()
        while code in data["students"]:
            code = "ST-" + secrets.token_hex(3).upper()
        data["students"][code] = {"id": code, "max_user_id": key, "user_id": key, "group_name": group,
                                  "full_name": fio, "invite_code": code, "created_at": int(time.time())}
        data["users"][key] = {"role": "student", "invite_code": code}
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        for p in PRACTICES:
            report_for(data, code, p)
        save_data(data)
        student_menu(uid, data, "Регистрация завершена.")
        return

    if payload == "REG_RETRY" and role == "student":
        data["temporary"].pop(key, None)
        register_student(uid, data)
        return
    if payload == "STUDENT_MENU" and role == "student":
        student_menu(uid, data)
        return
    if payload == "MY_CODE" and role == "student":
        code = student_code(uid, data)
        send_message(uid, "Ваш код для привязки: " + (code or "профиль не найден"))
        return
    if payload == "ASK_AI" and role in {"student", "teacher", "employer", "admin"}:
        data["states"][key] = "assistant_question"
        save_data(data)
        cancel_payload = {"student": "STUDENT_MENU", "teacher": "TEACHER_MENU",
                          "employer": "EMPLOYER_MENU", "admin": "ADMIN_MENU"}[role]
        send_message(uid, "Напишите вопрос. ИИ-помощник поможет с практикой, отчётами, учебными и организационными вопросами.",
                     [_button("Отмена", cancel_payload)])
        return
    if payload == "DIARY_MENU" and role == "student":
        code = student_code(uid, data)
        if code:
            _render_diary_menu(uid, code, data)
        return
    if payload.startswith("PRACTICE:") and role == "student":
        code = student_code(uid, data)
        if code:
            practice_screen(uid, code, payload.split(":", 1)[1], data)
        return
    if payload.startswith("SECTION:") and role == "student":
        try:
            _, practice, number = payload.split(":", 2)
        except ValueError:
            send_message(uid, "Некорректная команда раздела.")
            return
        code = student_code(uid, data)
        if number not in {"1", "2", "3", "4"} or practice not in REPORT_PRACTICES or not code:
            send_message(uid, "Неизвестный раздел отчёта.")
            return
        report = report_for(data, code, practice)
        if not practice_is_open(data, code, practice):
            send_message(uid, "Практика закрыта.")
            return
        if report.get("status") in {"submitted", "graded"}:
            value = report.get("sections", {}).get(number, "")
            file_items = report.get("files", {}).get(number, [])
            if isinstance(file_items, dict):
                file_items = [file_items]
            file_names = ", ".join(str(x.get("name") or "файл") for x in file_items if isinstance(x, dict))
            text_view = f"Раздел {number}:\n{value or ('Вложение: ' + (file_names or 'файл') if file_items else 'Пока не заполнен')}\n\nСтатус: {status_label(report.get('status'))}. Редактирование закрыто."
            send_message(uid, text_view, [_button("Назад к разделам", f"PRACTICE:{practice}")])
            for file_info in file_items if isinstance(file_items, list) else []:
                if isinstance(file_info, dict):
                    send_file_attachment(uid, file_info, f"{display_practice(practice)}, раздел {number}")
            return
        current_temp = data["temporary"].get(key, {})
        if data["states"].get(key) in {"section_ready", "section_text"} and current_temp.get("practice") == practice and str(current_temp.get("section")) == number:
            send_message(uid, "Этот раздел уже открыт. Отправьте текст или прикрепите документ в ответном сообщении.")
            return
        user["current_practice"] = practice
        # Старый JSON содержит подсказки и для первого трёх разделов; четвёртый берётся из fallback.
        data["temporary"][key] = {"practice": practice, "section": number}
        data["states"][key] = "section_ready"
        save_data(data)
        prompt = _section_hint(practice, number)
        rows = [_button("✍️ Ввести текст / прикрепить документ", f"SECTION_SAVE:{practice}:{number}"),
                _button("Назад к разделам", f"PRACTICE:{practice}")]
        send_message(uid, prompt, rows)
        return
    if payload.startswith("SECTION_SAVE:") and role == "student":
        try:
            _, practice, number = payload.split(":", 2)
        except ValueError:
            send_message(uid, "Некорректная команда.")
            return
        code = student_code(uid, data)
        if not code or practice not in REPORT_PRACTICES or number not in {"1", "2", "3", "4"}:
            send_message(uid, "Неизвестный раздел.")
            return
        report = report_for(data, code, practice)
        if not practice_is_open(data, code, practice) or report.get("status") != "draft":
            send_message(uid, "Этот отчёт уже нельзя редактировать.")
            return
        data["temporary"][key] = {"practice": practice, "section": number}
        data["states"][key] = "section_text"
        save_data(data)
        if number in ATTACHMENT_SECTIONS:
            send_message(uid, "Отправьте текст или прикрепите документ отдельным сообщением: нажмите на скрепку, выберите «Файл» и отправьте документ. Вложения поддерживаются в разделах 1, 2 и 4.")
        else:
            send_message(uid, "Введите текст раздела одним сообщением. Для раздела 3 прикрепление файлов не предусмотрено.")
        return
    if payload.startswith("SUBMIT:") and role == "student":
        practice = payload.split(":", 1)[1]
        code = student_code(uid, data)
        if not code or practice not in PRACTICES or not practice_is_open(data, code, practice):
            send_message(uid, "Отправка недоступна: практика закрыта.")
            return
        report = report_for(data, code, practice)
        if report.get("status") != "draft":
            send_message(uid, "Эта работа уже отправлена или оценена.")
            return
        if practice in REPORT_PRACTICES:
            missing = [str(i) for i in range(1, 5)
                       if not str(report["sections"].get(str(i), "")).strip() and not report["files"].get(str(i))]
            if missing:
                send_message(uid, "Нельзя отправить отчёт. Заполните разделы: " + ", ".join(missing))
                return
        elif not diary_for(data, code, practice):
            send_message(uid, "Добавьте хотя бы одну запись в дневник.")
            return
        report["status"] = "submitted"
        report["submitted_at"] = int(time.time())
        save_data(data)
        name = data["students"].get(code, {}).get("full_name", code)
        notify_linked(code, "teacher", data, f"Студент {name} отправил {display_practice(practice)} на проверку.")
        student_menu(uid, data, f"{display_practice(practice)} отправлена преподавателю. Редактирование закрыто.")
        return
    if payload.startswith("DIARY_ADD:") and role == "student":
        practice = payload.split(":", 1)[1]
        code = student_code(uid, data)
        if not code or practice not in DIARY_PRACTICES or not practice_is_open(data, code, practice):
            send_message(uid, "Этот дневник пока недоступен.")
            return
        if report_for(data, code, practice).get("status") != "draft":
            send_message(uid, "Дневник уже отправлен и закрыт для редактирования.")
            return
        user["current_practice"] = practice
        data["states"][key] = "diary_date"
        data["temporary"][key] = {"practice": practice}
        save_data(data)
        send_message(uid, "Введите дату записи в формате ДД.ММ.ГГГГ.")
        return
    if payload == "DIARY_ADD_DATE" and role == "student":
        code = student_code(uid, data)
        if code:
            _render_diary_menu(uid, code, data)
        return
    if payload.startswith("DIARY_VIEW:") and role == "student":
        parts = payload.split(":")
        practice = parts[1] if len(parts) > 1 else ""
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        code = student_code(uid, data)
        if code:
            _show_diary_page(uid, code, practice, page, data)
        return
    if payload == "DIARY_VIEW_ENTRIES" and role == "student":
        code = student_code(uid, data)
        if code:
            _render_diary_menu(uid, code, data)
        return
    if payload == "DIARY_EDIT" and role == "student" and data["states"].get(key) == "diary_confirm":
        temp = data["temporary"].get(key, {})
        code = student_code(uid, data)
        practice = temp.get("practice")
        if not code or practice not in DIARY_PRACTICES or not practice_is_open(data, code, practice) or report_for(data, code, practice).get("status") != "draft":
            send_message(uid, "Изменение недоступно: дневник закрыт.")
            return
        data["states"][key] = "diary_comment"
        save_data(data)
        send_message(uid, "Введите исправленный текст записи (от 5 до 1000 символов). Дата останется прежней.")
        return

    if payload == "DIARY_SAVE" and role == "student" and data["states"].get(key) == "diary_confirm":
        code = student_code(uid, data)
        temp = data["temporary"].get(key, {})
        practice = temp.get("practice")
        if not code or practice not in DIARY_PRACTICES or not practice_is_open(data, code, practice) or report_for(data, code, practice).get("status") != "draft":
            send_message(uid, "Сохранить нельзя: дневник уже закрыт.")
            return
        entries = diary_for(data, code, practice)
        row = {"entry_date": temp.get("entry_date"), "comment": temp.get("formatted_comment"),
               "pc_code": temp.get("pc_code"), "original_comment": temp.get("original_comment"),
               "created_at": int(time.time())}
        existing = next((x for x in entries if x.get("entry_date") == row["entry_date"]), None)
        if existing:
            existing.update(row)
        else:
            entries.append(row)
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        practice_screen(uid, code, practice, data)
        return
    if payload == "DIARY_CANCEL" and role == "student":
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Сохранение отменено.")
        return

    # Добавление студента сотрудником по invite_code.
    if payload in {"TEACHER_ADD_STUDENT", "EMPLOYER_ADD_STUDENT"} and role in {"teacher", "employer"}:
        data["states"][key] = "link_student"
        save_data(data)
        send_message(uid, "Введите пригласительный код студента (ST-XXXXXX):")
        return
    if payload in {"TEACHER_STUDENT_LIST", "EMPLOYER_STUDENT_LIST"} and role in {"teacher", "employer"}:
        codes = _list_linked_codes(uid, role, data)
        rows = [_button(data["students"][c].get("full_name", c), f"VIEW:{c}") for c in codes]
        send_message(uid, "Ваши студенты:" if codes else "Пока нет привязанных студентов.", rows or None)
        return
    if payload.startswith("TEACHER_FILTER:") and role == "teacher":
        wanted = payload.split(":", 1)[1]
        if wanted not in {"submitted", "graded"}:
            send_message(uid, "Неизвестный фильтр.")
            return
        rows = []
        for code in _list_linked_codes(uid, "teacher", data):
            name = data["students"][code].get("full_name", code)
            for practice in PRACTICES:
                report = report_for(data, code, practice)
                matches = (report.get("status") == "submitted" and not report.get("teacher_grade")) if wanted == "submitted" else bool(report.get("teacher_grade"))
                if matches:
                    rows.append(_button(f"{name} — {display_practice(practice)}", f"STAFF_PRACTICE:{code}:{practice}"))
        heading = "Работы, требующие проверки:" if wanted == "submitted" else "Проверенные работы:"
        send_message(uid, heading if rows else heading + "\nПодходящих работ пока нет.", rows or None)
        return
    if payload.startswith("VIEW:") and role in {"teacher", "employer", "admin"}:
        _staff_student_view(uid, payload.split(":", 1)[1], role, data)
        return
    if payload.startswith("STAFF_PRACTICE:") and role in {"teacher", "employer", "admin"}:
        try:
            _, code, practice = payload.split(":", 2)
        except ValueError:
            send_message(uid, "Некорректная команда просмотра.")
            return
        _staff_student_view(uid, code, role, data, practice)
        return
    if payload.startswith("TEACHER_COMMENT:") and role == "teacher":
        parts = payload.split(":")
        code = resolve_student_code(parts[1] if len(parts) > 1 else "", data)
        practice = parts[2] if len(parts) > 2 else ""
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "teacher"):
            send_message(uid, "Нет доступа к этому студенту или практике.")
            return
        report = report_for(data, code, practice)
        if report.get("status") not in {"submitted", "graded"}:
            send_message(uid, "Комментарий можно оставить после отправки работы студентом.")
            return
        user["selected_student_code"] = code
        user["current_practice"] = practice
        data["states"][key] = "teacher_comment"
        save_data(data)
        send_message(uid, f"Введите комментарий к работе по {display_practice(practice)} (от 5 до 1000 символов).")
        return

    if payload == "GRADE_SELECTED" and role == "teacher":
        code = user.get("selected_student_code")
        practice = user.get("current_practice")
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "teacher"):
            send_message(uid, "Сначала выберите привязанного студента и его практику.")
            return
        if report_for(data, code, practice).get("status") != "submitted":
            send_message(uid, "Оценивать можно только отправленную работу.")
            return
        data["states"][key] = "grade"
        save_data(data)
        send_message(uid, "Введите оценку: 3, 4, 5 или «Зачтено».")
        return
    if payload.startswith("GRADE:") and role == "teacher":
        parts = payload.split(":")
        code = parts[1] if len(parts) > 1 else ""
        practice = parts[2] if len(parts) > 2 else user.get("current_practice")
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "teacher"):
            send_message(uid, "Нет доступа к этому студенту.")
            return
        if report_for(data, code, practice).get("status") != "submitted":
            send_message(uid, "Оценивать можно только отправленную работу.")
            return
        user["selected_student_code"] = code
        user["current_practice"] = practice
        data["states"][key] = "grade"
        save_data(data)
        send_message(uid, f"Введите оценку за {display_practice(practice)}: 3, 4, 5 или «Зачтено».")
        return
    if payload.startswith("APPROVE:") and role == "employer":
        parts = payload.split(":")
        code = parts[1] if len(parts) > 1 else ""
        practice = parts[2] if len(parts) > 2 else user.get("current_practice")
        if not code or not practice or practice not in PRACTICES or not is_linked(uid, code, data, "employer"):
            send_message(uid, "Нет доступа к этому студенту или практике.")
            return
        report_for(data, code, practice)["employer_approved"] = True
        report_for(data, code, practice)["employer_approved_at"] = int(time.time())
        report_for(data, code, practice)["employer_approved_by"] = str(uid)
        save_data(data)
        send_message(uid, f"Подтверждение {display_practice(practice)} сохранено.")
        notify_student(code, data, f"Наставник подтвердил {display_practice(practice)}.")
        return
    if payload.startswith("EMPLOYER_NOTE:") and role == "employer":
        parts = payload.split(":")
        code = parts[1] if len(parts) > 1 else ""
        practice = parts[2] if len(parts) > 2 else user.get("current_practice")
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "employer"):
            send_message(uid, "Нет доступа к этому студенту.")
            return
        user["selected_student_code"] = code
        user["current_practice"] = practice
        data["states"][key] = "employer_note"
        save_data(data)
        send_message(uid, "Введите комментарий наставника (5–1000 символов) или «-», если без комментария.")
        return
    if payload in {"EMPLOYER_APPROVE_SELECTED", "EMPLOYER_NOTE_SELECTED"} and role == "employer":
        code = user.get("selected_student_code")
        practice = user.get("current_practice")
        if not code or not practice or not is_linked(uid, code, data, "employer"):
            send_message(uid, "Сначала выберите студента и конкретную практику.")
            return
        next_payload = f"APPROVE:{code}:{practice}" if payload == "EMPLOYER_APPROVE_SELECTED" else f"EMPLOYER_NOTE:{code}:{practice}"
        handle_callback(uid, next_payload, data)
        return

    # Управление группами и специальностями.
    if payload == "GROUP_MGMT" and role in {"teacher", "admin"}:
        _group_manager_menu(uid, data)
        return
    if payload == "GROUP_CREATE" and role in {"teacher", "admin"}:
        data["states"][key] = "group_create_name"
        data["temporary"][key] = {}
        save_data(data)
        send_message(uid, "Введите название новой группы (2–30 символов).")
        return
    if payload.startswith("GROUP_ITEM:") and role in {"teacher", "admin"}:
        group_id = payload.split(":", 1)[1]
        group = group_by_id(data, group_id)
        if not group or not can_manage_group(uid, role, group):
            send_message(uid, "Группа не найдена или у вас нет прав на её изменение.")
            return
        rows = [_button("✏️ Переименовать", f"GROUP_RENAME:{group_id}")]
        rows.append(_button("📦 Архивировать" if group.get("active", True) else "♻️ Восстановить", f"GROUP_TOGGLE:{group_id}"))
        rows.append(_button("Назад", "GROUP_MGMT"))
        send_message(uid, f"Группа: {group.get('name')}\nСпециальность: {group.get('specialty') or 'не указана'}\nСтатус: {'активна' if group.get('active', True) else 'в архиве'}", rows)
        return
    if payload.startswith("GROUP_RENAME:") and role in {"teacher", "admin"}:
        group_id = payload.split(":", 1)[1]
        group = group_by_id(data, group_id)
        if not group or not can_manage_group(uid, role, group):
            send_message(uid, "Нет доступа к этой группе.")
            return
        data["temporary"][key] = {"group_id": group_id}
        data["states"][key] = "group_rename"
        save_data(data)
        send_message(uid, "Введите новое название группы (2–30 символов). Исторические записи студентов сохранятся.")
        return
    if payload.startswith("GROUP_TOGGLE:") and role in {"teacher", "admin"}:
        group_id = payload.split(":", 1)[1]
        group = group_by_id(data, group_id)
        if not group or not can_manage_group(uid, role, group):
            send_message(uid, "Нет доступа к этой группе.")
            return
        group["active"] = not group.get("active", True)
        group["updated_at"] = int(time.time())
        save_data(data)
        send_message(uid, "Статус группы изменён. Существующие профили студентов и история сохранены.")
        _group_manager_menu(uid, data)
        return
    if payload == "SPECIALTY_CREATE" and role in {"teacher", "admin"}:
        data["states"][key] = "specialty_create"
        save_data(data)
        send_message(uid, "Введите название специальности (2–100 символов).")
        return
    if payload == "SPECIALTY_LIST" and role in {"teacher", "admin"}:
        rows = []
        for specialty in data.get("specialties", []):
            if isinstance(specialty, dict) and (role == "admin" or str(specialty.get("created_by")) == key):
                rows.append(_button(f"{specialty.get('name')} — {'активна' if specialty.get('active', True) else 'архив'}", f"SPECIALTY_ITEM:{specialty.get('id')}"))
        rows.append(_button("Назад", "GROUP_MGMT"))
        send_message(uid, "Специальности:", rows or None)
        return
    if payload.startswith("SPECIALTY_ITEM:") and role in {"teacher", "admin"}:
        specialty_id = payload.split(":", 1)[1]
        specialty = next((x for x in data.get("specialties", []) if isinstance(x, dict) and x.get("id") == specialty_id), None)
        if not specialty or (role != "admin" and str(specialty.get("created_by")) != key):
            send_message(uid, "Специальность не найдена или нет доступа.")
            return
        send_message(uid, f"Специальность: {specialty.get('name')}",
                     [_button("✏️ Переименовать", f"SPECIALTY_RENAME:{specialty_id}"),
                      _button("📦 Архивировать / восстановить", f"SPECIALTY_TOGGLE:{specialty_id}"),
                      _button("Назад", "SPECIALTY_LIST")])
        return
    if payload.startswith("SPECIALTY_RENAME:") and role in {"teacher", "admin"}:
        specialty_id = payload.split(":", 1)[1]
        specialty = next((x for x in data.get("specialties", []) if isinstance(x, dict) and x.get("id") == specialty_id), None)
        if not specialty or (role != "admin" and str(specialty.get("created_by")) != key):
            send_message(uid, "Нет доступа к специальности.")
            return
        data["temporary"][key] = {"specialty_id": specialty_id}
        data["states"][key] = "specialty_rename"
        save_data(data)
        send_message(uid, "Введите новое название специальности.")
        return
    if payload.startswith("SPECIALTY_TOGGLE:") and role in {"teacher", "admin"}:
        specialty_id = payload.split(":", 1)[1]
        specialty = next((x for x in data.get("specialties", []) if isinstance(x, dict) and x.get("id") == specialty_id), None)
        if not specialty or (role != "admin" and str(specialty.get("created_by")) != key):
            send_message(uid, "Нет доступа к специальности.")
            return
        specialty["active"] = not specialty.get("active", True)
        save_data(data)
        send_message(uid, "Статус специальности изменён.")
        _group_manager_menu(uid, data)
        return

    # Управление метаданными базовых практик. Их идентификаторы и порядок не меняются.
    if payload == "PRACTICE_MGMT" and role in {"teacher", "admin"}:
        _practice_manager_menu(uid, data)
        return
    if payload.startswith("PRACTICE_ITEM:") and role in {"teacher", "admin"}:
        practice = payload.split(":", 1)[1]
        if practice not in PRACTICES:
            send_message(uid, "Неизвестная практика.")
            return
        meta = practice_settings(data, practice)
        rows = [_button("✏️ Изменить название", f"PRACTICE_EDIT_TITLE:{practice}"),
                _button("📝 Изменить описание", f"PRACTICE_EDIT_DESC:{practice}"),
                _button("🔁 Изменить тип", f"PRACTICE_EDIT_TYPE:{practice}"),
                _button("Назад", "PRACTICE_MGMT")]
        send_message(uid, f"{display_practice(practice)}\nНазвание: {meta.get('title') or 'не задано'}\nОписание: {meta.get('description') or 'не задано'}\nТип: {meta.get('practice_type')}", rows)
        return
    for action, state_name, prompt in (
        ("PRACTICE_EDIT_TITLE:", "practice_edit_title", "Введите новое название (до 100 символов)."),
        ("PRACTICE_EDIT_DESC:", "practice_edit_desc", "Введите новое описание практики (до 1000 символов)."),
        ("PRACTICE_EDIT_TYPE:", "practice_edit_type", "Введите тип: учебная, производственная или учебная и производственная."),
    ):
        if payload.startswith(action) and role in {"teacher", "admin"}:
            practice = payload.split(":", 1)[1]
            if practice not in PRACTICES:
                send_message(uid, "Неизвестная практика.")
                return
            data["temporary"][key] = {"practice": practice}
            data["states"][key] = state_name
            save_data(data)
            send_message(uid, prompt)
            return
    if payload == "CUSTOM_PRACTICE_CREATE" and role in {"teacher", "admin"}:
        data["states"][key] = "custom_practice_title"
        data["temporary"][key] = {}
        save_data(data)
        send_message(uid, "Введите название новой практики. Она сохранится в каталоге, но пока не будет включена в маршрут студента.")
        return
    if payload.startswith("CUSTOM_PRACTICE_ITEM:") and role in {"teacher", "admin"}:
        item_id = payload.split(":", 1)[1]
        item = next((x for x in data.get("custom_practices", []) if isinstance(x, dict) and x.get("id") == item_id), None)
        if not item or (role != "admin" and str(item.get("created_by")) != key):
            send_message(uid, "Практика не найдена или нет доступа.")
            return
        send_message(uid, f"{item.get('title')}\nОписание: {item.get('description') or 'не задано'}\nТип: {item.get('practice_type') or 'не указан'}\nСтатус: {'активна в каталоге' if item.get('active', True) else 'архив'}\nПодключение к сдаче: пока не реализовано.",
                     [_button("✏️ Изменить название", f"CUSTOM_PRACTICE_RENAME:{item_id}"),
                      _button("✏️ Изменить описание", f"CUSTOM_PRACTICE_DESC:{item_id}"),
                      _button("📦 Архивировать / восстановить", f"CUSTOM_PRACTICE_TOGGLE:{item_id}"),
                      _button("Назад", "PRACTICE_MGMT")])
        return
    for action, state_name, prompt in (
        ("CUSTOM_PRACTICE_RENAME:", "custom_practice_rename", "Введите новое название."),
        ("CUSTOM_PRACTICE_DESC:", "custom_practice_desc", "Введите описание практики."),
    ):
        if payload.startswith(action) and role in {"teacher", "admin"}:
            item_id = payload.split(":", 1)[1]
            item = next((x for x in data.get("custom_practices", []) if isinstance(x, dict) and x.get("id") == item_id), None)
            if not item or (role != "admin" and str(item.get("created_by")) != key):
                send_message(uid, "Нет доступа к этой практике.")
                return
            data["temporary"][key] = {"custom_practice_id": item_id}
            data["states"][key] = state_name
            save_data(data)
            send_message(uid, prompt)
            return
    if payload.startswith("CUSTOM_PRACTICE_TOGGLE:") and role in {"teacher", "admin"}:
        item_id = payload.split(":", 1)[1]
        item = next((x for x in data.get("custom_practices", []) if isinstance(x, dict) and x.get("id") == item_id), None)
        if not item or (role != "admin" and str(item.get("created_by")) != key):
            send_message(uid, "Нет доступа к этой практике.")
            return
        item["active"] = not item.get("active", True)
        save_data(data)
        send_message(uid, "Статус записи каталога изменён. История базовых практик не затронута.")
        _practice_manager_menu(uid, data)
        return

    # Конструктор разделов — подготовительная часть. Поля и права уже сохраняются,
    # но включение этих разделов в сдаваемый отчёт отложено до отдельной миграции валидации.
    if payload == "CUSTOM_SECTIONS_MGMT" and role in {"teacher", "admin"}:
        if role == "teacher":
            groups = {data["students"][c].get("group_name") for c in _list_linked_codes(uid, "teacher", data)
                      if c in data["students"] and data["students"][c].get("group_name")}
            groups.update(x.get("name") for x in data.get("managed_groups", [])
                          if isinstance(x, dict) and str(x.get("created_by")) == key and x.get("active", True))
            groups = sorted(x for x in groups if x)
        else:
            groups = sorted({x.get("group_name") for x in data.get("students", {}).values()
                             if isinstance(x, dict) and x.get("group_name")})
            groups = sorted(set(groups) | {x.get("name") for x in data.get("managed_groups", [])
                                           if isinstance(x, dict) and x.get("active", True)})
        rows = [_button(f"Группа {g}", "CUSTOM_SECTION_GROUP:" + str(g)) for g in groups]
        rows.append(_button("Назад", "TEACHER_MENU" if role == "teacher" else "ADMIN_MENU"))
        send_message(uid, "Выберите группу для дополнительных разделов. Настройки сохраняются отдельно, но пока не показываются студентам и не влияют на обязательность отправки отчёта.", rows or None)
        return
    if payload.startswith("CUSTOM_SECTION_GROUP:") and role in {"teacher", "admin"}:
        group_name = payload.split(":", 1)[1]
        data["temporary"][key] = {"custom_section_group": group_name}
        rows = [_button(display_practice(p), f"CUSTOM_SECTION_PRACTICE:{p}") for p in sorted(REPORT_PRACTICES)]
        rows.append(_button("Назад", "CUSTOM_SECTIONS_MGMT"))
        send_message(uid, f"Группа: {group_name}. Выберите практику отчётного типа:", rows)
        return
    if payload.startswith("CUSTOM_SECTION_PRACTICE:") and role in {"teacher", "admin"}:
        practice = payload.split(":", 1)[1]
        if practice not in REPORT_PRACTICES:
            send_message(uid, "Для дневников дополнительные разделы пока не настраиваются.")
            return
        temp = data["temporary"].setdefault(key, {})
        temp["custom_section_practice"] = practice
        rows = [_button(f"{x.get('section_title')} — {'активен' if x.get('active', True) else 'архив'}", f"CUSTOM_SECTION_ITEM:{x.get('id')}")
                for x in custom_sections_for_group(data, temp.get("custom_section_group", ""), practice)
                if role == "admin" or str(x.get("teacher_id")) == key]
        rows.append(_button("➕ Добавить раздел", "CUSTOM_SECTION_ADD"))
        rows.append(_button("Назад", "CUSTOM_SECTIONS_MGMT"))
        save_data(data)
        send_message(uid, f"Дополнительные разделы для {display_practice(practice)}. Сейчас они сохраняются как заготовка и не включены в студенческий отчёт:", rows)
        return
    if payload == "CUSTOM_SECTION_ADD" and role in {"teacher", "admin"}:
        temp = data.get("temporary", {}).get(key, {})
        if not temp.get("custom_section_group") or temp.get("custom_section_practice") not in REPORT_PRACTICES:
            send_message(uid, "Сначала выберите группу и практику.")
            return
        data["states"][key] = "custom_section_title"
        save_data(data)
        send_message(uid, "Введите название дополнительного раздела (до 150 символов).")
        return
    if payload.startswith("CUSTOM_SECTION_ITEM:") and role in {"teacher", "admin"}:
        section_id = payload.split(":", 1)[1]
        section = next((x for x in data.get("custom_sections", []) if isinstance(x, dict) and x.get("id") == section_id), None)
        if not section or (role != "admin" and str(section.get("teacher_id")) != key):
            send_message(uid, "Нет доступа к этому разделу.")
            return
        rows = [
            _button("✏️ Изменить название", f"CUSTOM_SECTION_EDIT_TITLE:{section_id}"),
            _button("✏️ Изменить подсказку", f"CUSTOM_SECTION_EDIT_HINT:{section_id}"),
            _button("📦 Архивировать" if section.get("active", True) else "♻️ Восстановить",
                    f"CUSTOM_SECTION_TOGGLE:{section_id}"),
            _button("Назад", f"CUSTOM_SECTION_PRACTICE:{section.get('pm_code')}"),
        ]
        send_message(uid, f"{section.get('section_title')}\nПодсказка: {section.get('section_hint') or 'не задана'}\nСтатус: {'активен' if section.get('active', True) else 'архив'}\n\nРаздел пока не подключён к студенческому отчёту.", rows)
        return
    for action, state_name, prompt in (
        ("CUSTOM_SECTION_EDIT_TITLE:", "custom_section_edit_title", "Введите новое название раздела (2–150 символов)."),
        ("CUSTOM_SECTION_EDIT_HINT:", "custom_section_edit_hint", "Введите новую подсказку (до 1000 символов) или «-», чтобы очистить её."),
    ):
        if payload.startswith(action) and role in {"teacher", "admin"}:
            section_id = payload.split(":", 1)[1]
            section = next((x for x in data.get("custom_sections", []) if isinstance(x, dict) and x.get("id") == section_id), None)
            if not section or (role != "admin" and str(section.get("teacher_id")) != key):
                send_message(uid, "Нет доступа к этому разделу.")
                return
            data["temporary"][key] = {"custom_section_id": section_id}
            data["states"][key] = state_name
            save_data(data)
            send_message(uid, prompt)
            return
    if payload.startswith("CUSTOM_SECTION_TOGGLE:") and role in {"teacher", "admin"}:
        section_id = payload.split(":", 1)[1]
        section = next((x for x in data.get("custom_sections", []) if isinstance(x, dict) and x.get("id") == section_id), None)
        if not section or (role != "admin" and str(section.get("teacher_id")) != key):
            send_message(uid, "Нет доступа к этому разделу.")
            return
        section["active"] = not section.get("active", True)
        save_data(data)
        handle_callback(uid, f"CUSTOM_SECTION_PRACTICE:{section.get('pm_code')}", data)
        return

    # Старые кнопки создания заявок студентом отключены; заявки сотрудников остаются доступны.
    if role == "student" and (payload in {"MY_REQUESTS", "REQUEST_CREATE"} or payload.startswith("REQUEST_KIND:")):
        send_message(uid, "Создание заявок через меню студента отключено.", [_button("В меню", "STUDENT_MENU")])
        return

    if payload == "STAFF_REQUESTS" and role in {"teacher", "admin"}:
        linked = set(data["students"].keys()) if role == "admin" else set(_list_linked_codes(uid, "teacher", data))
        items = [x for x in data.get("requests", []) if isinstance(x, dict) and x.get("student_code") in linked]
        rows = [_button(f"#{x.get('id')} {x.get('kind')} — {x.get('status')}", f"REQUEST_VIEW:{x.get('id')}") for x in items[-20:]]
        rows.append(_button("Назад", "TEACHER_MENU" if role == "teacher" else "ADMIN_MENU"))
        send_message(uid, "Заявки студентов:", rows or None)
        return
    if payload.startswith("REQUEST_VIEW:") and role in {"teacher", "admin"}:
        request_id = payload.split(":", 1)[1]
        item = next((x for x in data.get("requests", []) if isinstance(x, dict) and str(x.get("id")) == request_id), None)
        if not item or (role == "teacher" and not is_linked(uid, item.get("student_code"), data, "teacher")):
            send_message(uid, "Заявка не найдена или нет доступа.")
            return
        student = data["students"].get(item.get("student_code"), {})
        rows = [_button("✍️ Ответить", f"REQUEST_REPLY:{request_id}"), _button("Назад", "STAFF_REQUESTS")]
        send_message(uid, f"Заявка #{request_id}\nСтудент: {student.get('full_name', '—')} ({student.get('group_name', '—')})\nТип: {item.get('kind')}\nСтатус: {item.get('status')}\n\n{item.get('description')}\n\nОтвет: {item.get('response') or 'нет'}", rows)
        return
    if payload.startswith("REQUEST_REPLY:") and role in {"teacher", "admin"}:
        request_id = payload.split(":", 1)[1]
        item = next((x for x in data.get("requests", []) if isinstance(x, dict) and str(x.get("id")) == request_id), None)
        if not item or (role == "teacher" and not is_linked(uid, item.get("student_code"), data, "teacher")):
            send_message(uid, "Нет доступа к заявке.")
            return
        data["temporary"][key] = {"request_id": request_id}
        data["states"][key] = "request_response"
        save_data(data)
        send_message(uid, "Введите ответ студенту (5–1000 символов).")
        return

    if payload == "REMINDER_MENU" and role in {"teacher", "admin"}:
        rows = [_button(f"Напомнить о {display_practice(p)}", f"REMIND:{p}") for p in sorted(DIARY_PRACTICES)]
        rows.append(_button("Назад", "TEACHER_MENU" if role == "teacher" else "ADMIN_MENU"))
        send_message(uid, "Выберите дневник для напоминания. Отправка будет выполнена привязанным студентам.", rows)
        return
    if payload.startswith("REMIND:") and role in {"teacher", "admin"}:
        practice = payload.split(":", 1)[1]
        if practice not in DIARY_PRACTICES:
            send_message(uid, "Неизвестный дневник.")
            return
        codes = set(data["students"].keys()) if role == "admin" else set(_list_linked_codes(uid, "teacher", data))
        sent = 0
        for code in codes:
            report = report_for(data, code, practice)
            if report.get("status") != "graded":
                target = data["students"].get(code, {}).get("max_user_id")
                if target:
                    send_message(target, f"Напоминание: проверьте дневник {display_practice(practice)} и заполните записи о практике.")
                    sent += 1
        send_message(uid, f"Напоминание отправлено студентам: {sent}.")
        return

    if payload.startswith("EXPORT_REPORT:") and role in {"teacher", "admin"}:
        try:
            _, code, practice = payload.split(":", 2)
        except ValueError:
            send_message(uid, "Некорректная команда выгрузки.")
            return
        code = resolve_student_code(code, data)
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, role):
            send_message(uid, "Нет доступа к этой работе.")
            return
        student = data["students"].get(code, {})
        safe_name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "_", str(student.get('full_name', 'student'))).strip("_")[:40]
        filename = f"{safe_name}_{practice}.txt"
        content = _report_text_for_export(code, practice, data).encode("utf-8")
        _send_generated_file(uid, filename, content, f"Выгрузка: {practice_label(practice, data)} — {student.get('full_name', 'студент')}")
        return

    if payload == "ADMIN_EXPORT_XLSX" and role == "admin":
        _export_admin_xlsx(uid, data)
        return

    if payload == "ADMIN_STATS" and role == "admin":
        total = len(data["students"])
        passed = sum(all(report_for(data, c, p).get("teacher_grade") for p in PRACTICES) for c in data["students"])
        values = {
            "total_students": total, "passed_students": passed,
            "failed_students": total - passed,
            "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        if isinstance(ADMIN_FLOW.get("ADMIN_STATS"), dict):
            flow_screen(uid, ADMIN_FLOW, "ADMIN_STATS", values)
        else:
            send_message(uid, f"Статистика практики\nВсего студентов: {total}\nСдали все 8 практик: {passed}\nЕщё не сдали: {total-passed}\nОбновлено: {values['updated_at']}",
                         [_button("Обновить", "ADMIN_STATS"), _button("Назад", "ADMIN_MENU")])
        return
    if payload in {"TEACHER_MENU", "EMPLOYER_MENU", "ADMIN_MENU"} and role in {"teacher", "employer", "admin"}:
        staff_menu(uid, role)
        return

    # Старые JSON-команды, для которых в старом файле нет отдельного экрана.
    if payload.startswith("PM_") and role == "student":
        send_message(uid, "Эта кнопка устаревшего меню. Откройте /start и выберите практику в основном меню.")
        return
    send_message(uid, "Эта кнопка устарела или пока не подключена. Напишите /start и откройте нужный раздел заново.")


def _extract_file_attachments(message: dict | None) -> list[dict]:
    if not isinstance(message, dict):
        return []
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    raw = body.get("attachments") or message.get("attachments") or []
    found = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("kind") or "file").lower()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        # MAX file payloads normally expose token/url/filename directly. The
        # aliases also tolerate older update shapes without logging file tokens.
        nested_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        token = (payload.get("token") or item.get("token") or payload.get("file_id")
                 or item.get("file_id") or nested_file.get("token"))
        url = payload.get("url") or item.get("url") or nested_file.get("url")
        filename = (payload.get("filename") or payload.get("name") or item.get("filename")
                    or item.get("name") or nested_file.get("filename") or nested_file.get("name"))
        size = payload.get("size") or item.get("size") or nested_file.get("size")
        if token or url or kind in {"file", "document", "image", "video", "audio"}:
            found.append({"type": kind, "token": token, "url": url, "name": filename, "size": size})
    return found


def _finish_diary_ai(uid: str, original: str, practice: str, result: Any) -> None:
    if isinstance(result, dict):
        normalized = result
    else:
        normalized = {"pc_code": None, "formal_text": str(result or original), "ai_used": False}
    AI_RESULTS.put((uid, original, practice, normalized))


def process_ai_results() -> None:
    while True:
        try:
            uid, original, practice, result = AI_RESULTS.get_nowait()
        except queue.Empty:
            break
        data = load_data()
        temp = data.get("temporary", {}).get(uid, {})
        if data.get("states", {}).get(uid) != "diary_ai_pending" or temp.get("original_comment") != original:
            continue
        if practice != temp.get("practice"):
            continue
        pc_code = result.get("pc_code") if isinstance(result, dict) else None
        formal = str((result or {}).get("formal_text", "") if isinstance(result, dict) else "").strip() or original
        # Повторная проверка кода ПК на стороне бота — не доверяем только ответу модели.
        allowed = set()
        try:
            competency_path = BASE_DIR / "pm_competencies.json"
            competency_data = json.loads(competency_path.read_text(encoding="utf-8"))
            allowed = {str(x.get("code")) for x in competency_data.get(practice, {}).get("competencies", []) if x.get("code")}
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            allowed = set()
        if pc_code not in allowed:
            pc_code = None
        temp["pc_code"] = pc_code
        temp["formatted_comment"] = formal[:1000]
        data["states"][uid] = "diary_confirm"
        save_data(data)
        pc_line = f"Компетенция: {pc_code}" if pc_code else "Компетенция: пока не определена"
        send_message(
            uid,
            "Проверьте запись перед сохранением:\n\n" + pc_line + "\n\n" + temp["formatted_comment"],
            [[{"type": "callback", "text": "✅ Подтвердить", "payload": "DIARY_SAVE"}],
             [{"type": "callback", "text": "✏️ Изменить текст", "payload": "DIARY_EDIT"}],
             [{"type": "callback", "text": "Отмена", "payload": "DIARY_CANCEL"}]],
        )


    while True:
        try:
            uid, question, answer = AI_HELP_RESULTS.get_nowait()
        except queue.Empty:
            break
        data = load_data()
        temp = data.get("temporary", {}).get(uid, {})
        if data.get("states", {}).get(uid) != "assistant_ai_pending" or temp.get("assistant_question") != question:
            continue
        data["states"].pop(uid, None)
        data.get("temporary", {}).pop(uid, None)
        save_data(data)
        role = role_of(uid, data)
        menu_payload = {"student": "STUDENT_MENU", "teacher": "TEACHER_MENU",
                        "employer": "EMPLOYER_MENU", "admin": "ADMIN_MENU"}.get(role, "STUDENT_MENU")
        send_message(uid, "Ответ ИИ-помощника:\n\n" + answer[:3500],
                     [_button("Задать ещё вопрос", "ASK_AI"), _button("В меню", menu_payload)])


def notify_linked(code: str, role: str, data: dict, message: str) -> None:
    notified = set()
    for row in data["links"]:
        staff_id = str(row.get("staff_id", ""))
        if _link_student_code(row, data) == code and row.get("staff_type") == role and staff_id not in notified:
            notified.add(staff_id)
            send_message(staff_id, message)


def notify_student(code: str, data: dict, message: str) -> None:
    student = data["students"].get(code, {})
    uid = student.get("max_user_id", student.get("user_id"))
    if uid is not None:
        send_message(uid, message)


def handle_text(uid: Any, raw_text: str, data: dict, message: dict | None = None) -> None:
    key = str(uid)
    text = (raw_text or "").strip()
    user = data["users"].setdefault(key, {})
    role = role_of(uid, data)
    state = data["states"].get(key)
    attachments = _extract_file_attachments(message)

    if text.lower() in {"/logout", "выход", "выйти"}:
        logout_staff(uid, data)
        return

    if text.lower() in {"/start", "старт", "начать"}:
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        start(uid, data)
        return

    if state == "staff_access_code":
        requested_role = data.get("temporary", {}).get(key, {}).get("requested_role")
        existing = role_of(uid, data)
        if requested_role not in STAFF_ROLES:
            data["states"].pop(key, None)
            data["temporary"].pop(key, None)
            save_data(data)
            send_message(uid, "Сценарий авторизации устарел. Нажмите /start и выберите роль заново.")
            return
        if existing and existing != requested_role:
            send_message(uid, "За аккаунтом MAX уже закреплена другая роль. Текущий код не был использован.")
            return
        verified, reason = security_manager.verify_and_consume_access_code(
            data, key, requested_role, text
        )
        if not verified:
            messages = {
                "blocked": "Слишком много неверных попыток. Ввод кода временно заблокирован на 15 минут.",
                "expired": "Срок действия кода истёк. Запросите новый код у ответственного за ПрактикБот.",
                "wrong_role": "Код не подходит к выбранной роли. Проверьте роль или запросите правильный код.",
                "invalid": "Код не подошёл или уже использован. Проверьте его либо запросите новый.",
            }
            send_message(uid, messages.get(reason, messages["invalid"]))
            save_data(data)
            return
        _finish_staff_registration(uid, requested_role, data)
        return

    if role in STAFF_ROLES and not _staff_is_authorized(uid, role, data):
        begin_staff_auth(uid, role, data)
        return

    if role == "admin" and state and state not in {
        "staff_fio", "group_create_name", "group_create_specialty", "group_rename",
        "specialty_create", "specialty_rename", "request_response",
        "assistant_question", "assistant_ai_pending",
    }:
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Старый незавершённый сценарий закрыт. Откройте нужный раздел в панели администратора.")
        return

    if state == "staff_fio":
        if role not in {"teacher", "employer", "admin"}:
            data["states"].pop(key, None)
            save_data(data)
            send_message(uid, "Регистрация сотрудника сброшена. Напишите /start.")
            return
        if not valid_fio(text):
            send_message(uid, "Введите ФИО: ровно три слова русскими буквами, дефис допускается.")
            return
        fio = normalize_fio(text)
        user["full_name"] = fio
        data[role + "s"][key]["fio"] = fio
        if role == "employer":
            data["states"][key] = "employer_position"
            save_data(data)
            send_message(uid, "Введите вашу должность (например: наставник, мастер, инженер).")
            return
        data["states"].pop(key, None)
        save_data(data)
        staff_menu(uid, role)
        return

    if state == "employer_position":
        if len(text) < 2 or len(text) > 100:
            send_message(uid, "Укажите должность длиной от 2 до 100 символов.")
            return
        user["position"] = text
        data["employers"][key]["position"] = text
        data["states"].pop(key, None)
        save_data(data)
        staff_menu(uid, "employer")
        return

    if state == "student_group":
        # Для клиентов, которые отправили название группы текстом, а не нажали кнопку.
        if text not in available_groups(data):
            send_message(uid, "Такой группы нет в списке. Нажмите «Ввести другую группу» или выберите существующую.",
                         [_button("Ввести другую группу", "GROUP_OTHER")])
            return
        data["temporary"][key] = {"group_name": text}
        data["states"][key] = "student_fio"
        save_data(data)
        send_message(uid, "Введите ФИО полностью (фамилия, имя, отчество).")
        return

    if state == "student_group_custom":
        group = _clean_short_text(text, 30)
        if len(group) < 2 or not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _-]{2,30}", group):
            send_message(uid, "Название группы должно содержать 2–30 символов: буквы, цифры, пробел, дефис или подчёркивание.")
            return
        data["temporary"][key] = {"group_name": group, "manual_group": True}
        data["states"][key] = "student_fio"
        save_data(data)
        send_message(uid, "Введите ФИО полностью (фамилия, имя, отчество).")
        return

    if state == "student_fio":
        if not valid_fio(text):
            send_message(uid, "Введите ровно три слова русскими буквами (дефис допускается), не более 100 символов.")
            return
        group = data["temporary"].get(key, {}).get("group_name")
        fio = normalize_fio(text)
        if not group:
            send_message(uid, "Сессия регистрации устарела. Напишите /start.")
            return
        data["temporary"][key]["fio"] = fio
        data["states"][key] = "registration_confirm"
        save_data(data)
        send_message(uid, f"Проверьте данные:\nГруппа: {group}\nФИО: {fio}\n\nПодтвердить регистрацию?",
                     [[{"type": "callback", "text": "Подтвердить", "payload": "REG_CONFIRM"},
                       {"type": "callback", "text": "Изменить", "payload": "REG_RETRY"}]])
        return

    if state == "group_create_name":
        name = _clean_short_text(text, 30)
        if len(name) < 2 or not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _-]{2,30}", name) or name.casefold() in {x.casefold() for x in available_groups(data)}:
            send_message(uid, "Название должно быть от 2 до 30 символов, без специальных символов, и не повторять существующую группу.")
            return
        data["temporary"][key] = {"group_name": name}
        data["states"][key] = "group_create_specialty"
        save_data(data)
        send_message(uid, "Введите специальность группы (или отправьте «-», если пока не указана).")
        return
    if state == "group_create_specialty":
        temp = data.get("temporary", {}).get(key, {})
        name = temp.get("group_name")
        specialty = "" if text == "-" else _clean_short_text(text, 100)
        if not name:
            send_message(uid, "Сессия создания группы устарела. Откройте управление группами заново.")
            return
        group_id = "GR-" + secrets.token_hex(4).upper()
        group = {"id": group_id, "name": name, "specialty": specialty,
                 "created_by": key, "created_role": role, "active": True,
                 "created_at": int(time.time())}
        data.setdefault("managed_groups", []).append(group)
        if specialty and not any(str(x.get("name", "")).casefold() == specialty.casefold() for x in data.get("specialties", []) if isinstance(x, dict)):
            data.setdefault("specialties", []).append({"id": "SP-" + secrets.token_hex(4).upper(),
                                                         "name": specialty, "created_by": key,
                                                         "active": True, "created_at": int(time.time())})
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, f"Группа {name} создана и доступна для регистрации студентов.")
        _group_manager_menu(uid, data)
        return
    if state == "group_rename":
        temp = data.get("temporary", {}).get(key, {})
        group = group_by_id(data, temp.get("group_id", ""))
        name = _clean_short_text(text, 30)
        if not group or not can_manage_group(uid, role, group):
            send_message(uid, "Нет доступа к группе.")
            return
        if len(name) < 2:
            send_message(uid, "Название должно содержать от 2 до 30 символов.")
            return
        old_name = group.get("name")
        if name.casefold() in {x.casefold() for x in available_groups(data) if x.casefold() != str(old_name).casefold()}:
            send_message(uid, "Такое название уже используется.")
            return
        group["name"] = name
        # Исторические студенческие записи переводятся на новое отображаемое имя группы.
        for student in data.get("students", {}).values():
            if isinstance(student, dict) and student.get("group_name") == old_name:
                student["group_name"] = name
        for section in data.get("custom_sections", []):
            if isinstance(section, dict) and section.get("group_name") == old_name:
                section["group_name"] = name
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Группа переименована. История студентов сохранена.")
        _group_manager_menu(uid, data)
        return
    if state == "specialty_create":
        name = _clean_short_text(text, 100)
        if len(name) < 2:
            send_message(uid, "Название специальности должно содержать от 2 до 100 символов.")
            return
        if any(str(x.get("name", "")).casefold() == name.casefold() and x.get("active", True) for x in data.get("specialties", []) if isinstance(x, dict)):
            send_message(uid, "Такая специальность уже есть.")
            return
        data.setdefault("specialties", []).append({"id": "SP-" + secrets.token_hex(4).upper(),
                                                     "name": name, "created_by": key,
                                                     "active": True, "created_at": int(time.time())})
        data["states"].pop(key, None)
        save_data(data)
        send_message(uid, "Специальность добавлена.")
        _group_manager_menu(uid, data)
        return
    if state == "specialty_rename":
        specialty_id = data.get("temporary", {}).get(key, {}).get("specialty_id")
        specialty = next((x for x in data.get("specialties", []) if isinstance(x, dict) and x.get("id") == specialty_id), None)
        name = _clean_short_text(text, 100)
        if not specialty or (role != "admin" and str(specialty.get("created_by")) != key) or len(name) < 2:
            send_message(uid, "Не удалось изменить специальность. Проверьте права и название.")
            return
        old_name = specialty.get("name")
        specialty["name"] = name
        for group in data.get("managed_groups", []):
            if isinstance(group, dict) and group.get("specialty") == old_name:
                group["specialty"] = name
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Специальность переименована.")
        _group_manager_menu(uid, data)
        return
    if state in {"practice_edit_title", "practice_edit_desc", "practice_edit_type"}:
        practice = data.get("temporary", {}).get(key, {}).get("practice")
        if practice not in PRACTICES:
            send_message(uid, "Сессия редактирования практики устарела.")
            return
        meta = practice_settings(data, practice)
        if state == "practice_edit_title":
            if len(text) < 2 or len(text) > 100:
                send_message(uid, "Название должно содержать от 2 до 100 символов.")
                return
            meta["title"] = text
        elif state == "practice_edit_desc":
            if len(text) < 2 or len(text) > 1000:
                send_message(uid, "Описание должно содержать от 2 до 1000 символов.")
                return
            meta["description"] = text
        else:
            value = text.casefold()
            allowed = {"учебная": "учебная", "производственная": "производственная",
                       "учебная и производственная": "учебная и производственная"}
            if value not in allowed:
                send_message(uid, "Введите один из вариантов: учебная, производственная, учебная и производственная.")
                return
            meta["practice_type"] = allowed[value]
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Данные практики обновлены. История и оценки не изменены.")
        _practice_manager_menu(uid, data)
        return
    if state == "custom_practice_title":
        title = _clean_short_text(text, 100)
        if len(title) < 2:
            send_message(uid, "Название должно содержать от 2 до 100 символов.")
            return
        data["temporary"][key] = {"title": title}
        data["states"][key] = "custom_practice_desc"
        save_data(data)
        send_message(uid, "Введите описание практики (или «-», если пока нет).")
        return
    if state == "custom_practice_desc":
        temp = data.get("temporary", {}).get(key, {})
        title = temp.get("title")
        if not title:
            send_message(uid, "Сессия создания практики устарела.")
            return
        data["temporary"][key]["description"] = "" if text == "-" else text[:1000]
        data["states"][key] = "custom_practice_type"
        save_data(data)
        send_message(uid, "Введите тип: учебная, производственная или учебная и производственная.")
        return
    if state == "custom_practice_type":
        temp = data.get("temporary", {}).get(key, {})
        allowed = {"учебная", "производственная", "учебная и производственная"}
        value = text.casefold()
        if value not in allowed:
            send_message(uid, "Введите: учебная, производственная или учебная и производственная.")
            return
        data.setdefault("custom_practices", []).append({"id": "CP-" + secrets.token_hex(4).upper(),
            "title": temp.get("title"), "description": temp.get("description", ""),
            "practice_type": value, "created_by": key, "active": True,
            "workflow_enabled": False, "created_at": int(time.time())})
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Практика добавлена в каталог. Она пока не включена в маршрут сдачи, чтобы не нарушить последовательность восьми действующих практик.")
        _practice_manager_menu(uid, data)
        return
    if state in {"custom_practice_rename", "custom_practice_desc"}:
        item_id = data.get("temporary", {}).get(key, {}).get("custom_practice_id")
        item = next((x for x in data.get("custom_practices", []) if isinstance(x, dict) and x.get("id") == item_id), None)
        if not item or (role != "admin" and str(item.get("created_by")) != key):
            send_message(uid, "Нет доступа к записи практики.")
            return
        if state == "custom_practice_rename":
            if len(text) < 2 or len(text) > 100:
                send_message(uid, "Название должно содержать от 2 до 100 символов.")
                return
            item["title"] = text
        else:
            if len(text) < 2 or len(text) > 1000:
                send_message(uid, "Описание должно содержать от 2 до 1000 символов.")
                return
            item["description"] = text
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Запись каталога практик обновлена.")
        _practice_manager_menu(uid, data)
        return
    if state in {"custom_section_edit_title", "custom_section_edit_hint"}:
        section_id = data.get("temporary", {}).get(key, {}).get("custom_section_id")
        section = next((x for x in data.get("custom_sections", []) if isinstance(x, dict) and x.get("id") == section_id), None)
        if not section or (role != "admin" and str(section.get("teacher_id")) != key):
            send_message(uid, "Раздел не найден или у вас нет прав на его изменение.")
            return
        if state == "custom_section_edit_title":
            value = _clean_short_text(text, 150)
            if len(value) < 2:
                send_message(uid, "Название должно содержать от 2 до 150 символов.")
                return
            section["section_title"] = value
        else:
            if len(text) > 1000:
                send_message(uid, "Подсказка не должна превышать 1000 символов.")
                return
            section["section_hint"] = "" if text == "-" else text
        group_name = section.get("group_name", "")
        practice_code = section.get("pm_code", "")
        data["states"].pop(key, None)
        data["temporary"][key] = {"custom_section_group": group_name, "custom_section_practice": practice_code}
        save_data(data)
        send_message(uid, "Дополнительный раздел обновлён. Пока это настройка-заготовка, она не влияет на студенческую форму отчёта.")
        handle_callback(uid, f"CUSTOM_SECTION_PRACTICE:{practice_code}", data)
        return

    if state == "custom_section_title":
        title = _clean_short_text(text, 150)
        if len(title) < 2:
            send_message(uid, "Название раздела должно содержать от 2 до 150 символов.")
            return
        data["temporary"].setdefault(key, {})["section_title"] = title
        data["states"][key] = "custom_section_hint"
        save_data(data)
        send_message(uid, "Введите подсказку для студента (или «-», если подсказка не нужна).")
        return
    if state == "custom_section_hint":
        temp = data.get("temporary", {}).get(key, {})
        group_name = temp.get("custom_section_group")
        practice = temp.get("custom_section_practice")
        title = temp.get("section_title")
        if not group_name or practice not in REPORT_PRACTICES or not title:
            send_message(uid, "Настройка раздела устарела. Начните заново.")
            return
        data.setdefault("custom_sections", []).append({"id": "CS-" + secrets.token_hex(4).upper(),
            "teacher_id": key, "group_name": group_name, "pm_code": practice,
            "section_title": title, "section_hint": "" if text == "-" else text[:1000],
            "order_num": 100 + len(data.get("custom_sections", [])), "active": True,
            "created_at": int(time.time()), "workflow_enabled": False})
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Дополнительный раздел сохранён в настройках группы. Он пока не отображается студенту и не участвует в обязательной проверке отчёта.")
        return
    if state == "request_description" and role == "student":
        data["states"].pop(key, None)
        data.get("temporary", {}).pop(key, None)
        save_data(data)
        send_message(uid, "Создание заявок через меню студента отключено. Напишите /start, чтобы открыть меню.")
        return
    if state == "request_response":
        if not 5 <= len(text) <= 1000:
            send_message(uid, "Ответ должен содержать от 5 до 1000 символов.")
            return
        request_id = data.get("temporary", {}).get(key, {}).get("request_id")
        item = next((x for x in data.get("requests", []) if isinstance(x, dict) and str(x.get("id")) == request_id), None)
        if not item or (role == "teacher" and not is_linked(uid, item.get("student_code"), data, "teacher")):
            send_message(uid, "Заявка не найдена или нет доступа.")
            return
        item["response"] = text
        item["status"] = "ответ дан"
        item["responded_at"] = int(time.time())
        item["responded_by"] = key
        target = data.get("students", {}).get(item.get("student_code"), {}).get("max_user_id")
        if target:
            send_message(target, f"По вашей заявке {request_id} получен ответ:\n{text}")
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, "Ответ отправлен студенту.")
        return
    if state == "assistant_question":
        if not 5 <= len(text) <= 1500:
            send_message(uid, "Вопрос должен содержать от 5 до 1500 символов.")
            return
        data["states"][key] = "assistant_ai_pending"
        data["temporary"][key] = {"assistant_question": text}
        save_data(data)
        send_message(uid, "ИИ-помощник готовит ответ…")
        if answer_student_question:
            future = AI_POOL.submit(answer_student_question, text)
            def _assistant_done(future_result, user_id=key, question=text):
                try:
                    answer = str(future_result.result() or "")
                except Exception:
                    answer = "Не удалось получить ответ ИИ. Попробуйте позже."
                AI_HELP_RESULTS.put((user_id, question, answer))
            future.add_done_callback(_assistant_done)
        else:
            AI_HELP_RESULTS.put((key, text, "ИИ-помощник пока не подключён. Проверьте YANDEX_API_KEY и YANDEX_FOLDER_ID в .env."))
        return

    if state == "link_student":
        code = text.upper()
        if role not in {"teacher", "employer", "admin"}:
            send_message(uid, "Это действие доступно только сотрудникам.")
            return
        if code not in data["students"]:
            send_message(uid, "Код студента не найден. Проверьте его и отправьте ещё раз.")
            return
        add_link(uid, role, code, data)
        data["states"].pop(key, None)
        save_data(data)
        send_message(uid, "Студент добавлен в ваш список.")
        return

    if state in {"section_ready", "section_text"}:
        temp = data["temporary"].get(key, {})
        practice, number = temp.get("practice"), str(temp.get("section", ""))
        code = student_code(uid, data)
        if not code or practice not in REPORT_PRACTICES or number not in {"1", "2", "3", "4"}:
            send_message(uid, "Не удалось определить раздел. Откройте его заново через меню практики.")
            return
        if not practice_is_open(data, code, practice) or report_for(data, code, practice).get("status") != "draft":
            send_message(uid, "Нельзя изменить закрытый или уже отправленный отчёт.")
            return
        if attachments:
            if number not in ATTACHMENT_SECTIONS:
                send_message(uid, "В этот раздел документ прикреплять нельзя. Отправьте текст.")
                return
            usable_attachments = [x for x in attachments if x.get("token") or x.get("url")]
            if not usable_attachments:
                kinds = ",".join(str(x.get("type") or "unknown") for x in attachments)
                print(f"MAX incoming attachment has no token/url: uid={key}, types={kinds or 'unknown'}")
                send_message(uid, "MAX получил вложение, но не передал боту ссылку или токен файла. Попробуйте отправить документ как обычный файл (📎 → Файл), а не как пересланное сообщение или предпросмотр.")
                return
            report = report_for(data, code, practice)
            report["files"][number] = usable_attachments
            first_file = usable_attachments[0]
            if first_file.get("token"):
                report[f"section_{number}_file_id"] = first_file["token"]
            if first_file.get("url"):
                report[f"section_{number}_file_url"] = first_file["url"]
            if first_file.get("name"):
                report[f"section_{number}_file_name"] = first_file["name"]
            if text:
                report["sections"][number] = text
                report[f"section_{number}_text"] = text
        elif text:
            report_for(data, code, practice)["sections"][number] = text
            report_for(data, code, practice)[f"section_{number}_text"] = text
        else:
            send_message(uid, "Сообщение пустое. Отправьте текст или документ для этого раздела.")
            return
        data["states"].pop(key, None)
        data["temporary"].pop(key, None)
        save_data(data)
        send_message(uid, f"✅ Раздел {number} по {display_practice(practice)} сохранён.")
        practice_screen(uid, code, practice, data)
        return

    if state == "diary_date":
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
            send_message(uid, "Дата должна быть в формате ДД.ММ.ГГГГ, например 05.11.2026.")
            return
        try:
            parsed = datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            send_message(uid, "Дата должна быть в формате ДД.ММ.ГГГГ.")
            return
        if parsed > date.today():
            send_message(uid, "Дата не может быть в будущем.")
            return
        temp = data["temporary"].setdefault(key, {})
        temp["entry_date"] = parsed.strftime("%d.%m.%Y")
        data["states"][key] = "diary_comment"
        save_data(data)
        send_message(uid, "Опишите выполненную работу (от 5 до 1000 символов).")
        return

    if state == "diary_comment":
        if not 5 <= len(text) <= 1000:
            send_message(uid, "Описание должно содержать от 5 до 1000 символов.")
            return
        temp = data["temporary"].get(key, {})
        practice = temp.get("practice")
        code = student_code(uid, data)
        if not code or practice not in DIARY_PRACTICES or not practice_is_open(data, code, practice) or report_for(data, code, practice).get("status") != "draft":
            send_message(uid, "Дневник закрыт для редактирования.")
            return
        temp["original_comment"] = text
        data["states"][key] = "diary_ai_pending"
        save_data(data)
        if AI_CONFIGURED:
            send_message(uid, "Запись принята. ИИ подбирает формулировку и компетенцию…")
        else:
            send_message(uid, "ИИ сейчас не настроен. Подготовлен черновик без автоматического определения ПК; проверьте текст перед сохранением.")
        if analyze_work_description:
            future = AI_POOL.submit(analyze_work_description, text, practice)
            def _ai_done(future_result, user_id=key, original=text, practice_code=practice):
                try:
                    result = future_result.result()
                except Exception as exc:
                    print(f"YandexGPT assistant errer: {type(exc).__name__}")
                    result = {"pc_code": None, "formal_text": original, "ai_used": False}
                _finish_diary_ai(user_id, original, practice_code, result)
            future.add_done_callback(_ai_done)
        elif format_work_description:
            future = AI_POOL.submit(format_work_description, text)
            def _old_ai_done(future_result, user_id=key, original=text, practice_code=practice):
                try:
                    result = {"pc_code": None, "formal_text": future_result.result(), "ai_used": True}
                except Exception:
                    result = {"pc_code": None, "formal_text": original, "ai_used": False}
                _finish_diary_ai(user_id, original, practice_code, result)
            future.add_done_callback(_old_ai_done)
        else:
            _finish_diary_ai(key, text, practice, {"pc_code": None, "formal_text": text, "ai_used": False})
        return

    if state == "teacher_comment":
        code = user.get("selected_student_code")
        practice = user.get("current_practice")
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "teacher"):
            send_message(uid, "Нет доступа к этой работе. Выберите студента заново.")
            return
        if not 5 <= len(text) <= 1000:
            send_message(uid, "Комментарий должен содержать от 5 до 1000 символов.")
            return
        report = report_for(data, code, practice)
        if report.get("status") not in {"submitted", "graded"}:
            send_message(uid, "Комментарий можно оставить только к отправленной работе.")
            return
        report["teacher_comment"] = text
        report["teacher_comment_at"] = int(time.time())
        report["teacher_comment_by"] = str(uid)
        data["states"].pop(key, None)
        save_data(data)
        send_message(uid, "Комментарий преподавателя сохранён.")
        notify_student(code, data, f"Преподаватель оставил комментарий к {display_practice(practice)}. Откройте эту практику, чтобы прочитать его.")
        return

    if state == "grade":
        code = user.get("selected_student_code")
        practice = user.get("current_practice")
        grade_text = text
        parts = text.split()
        # Совместимость: допускаем как только оценку, так и «PM_01 5».
        if len(parts) == 2 and parts[0] in PRACTICES:
            practice, grade_text = parts
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "teacher"):
            send_message(uid, "Нет доступа к этому студенту. Выберите его заново из списка.")
            return
        if grade_text.lower() not in {"3", "4", "5", "зачтено"}:
            send_message(uid, "Допустимая оценка: 3, 4, 5 или «Зачтено».")
            return
        report = report_for(data, code, practice)
        if report.get("status") != "submitted":
            send_message(uid, "Оценивать можно только отправленную работу.")
            return
        report["teacher_grade"] = "Зачтено" if grade_text.lower() == "зачтено" else grade_text
        report["status"] = "graded"
        report["teacher_grade_at"] = int(time.time())
        report["graded_at"] = report["teacher_grade_at"]
        report["graded_by_teacher_id"] = str(uid)
        data["states"].pop(key, None)
        save_data(data)
        send_message(uid, f"Оценка {report['teacher_grade']} сохранена за {display_practice(practice)}.")
        notify_student(code, data, f"Преподаватель выставил оценку {report['teacher_grade']} за {display_practice(practice)}. Следующая практика открыта.")
        return

    if state == "employer_note":
        code = user.get("selected_student_code")
        practice = user.get("current_practice")
        if not code or practice not in PRACTICES or not is_linked(uid, code, data, "employer"):
            send_message(uid, "Нет доступа к этому студенту. Выберите его заново.")
            return
        if text != "-" and not 5 <= len(text) <= 1000:
            send_message(uid, "Комментарий должен содержать от 5 до 1000 символов или отправьте «-».")
            return
        report = report_for(data, code, practice)
        report["employer_note"] = "" if text == "-" else text
        report["employer_note_at"] = int(time.time())
        data["states"].pop(key, None)
        save_data(data)
        send_message(uid, "Комментарий наставника сохранён.")
        notify_student(code, data, f"Наставник оставил комментарий к {display_practice(practice)}. Откройте эту практику, чтобы прочитать его.")
        return

    send_message(uid, "Используйте кнопки меню или отправьте /start.")



def _update_fingerprint(update: dict) -> str | None:
    if update.get("update_id") is not None:
        return "update:" + str(update.get("update_id"))
    if update.get("update_type") == "message_callback":
        callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
        actor = callback.get("user") if isinstance(callback.get("user"), dict) else {}
        # callback_id в MAX — идентификатор клавиатуры, а не отдельного нажатия.
        # Для защиты от дублей используем время, пользователя и payload.
        timestamp = callback.get("timestamp") or update.get("timestamp")
        uid = actor.get("user_id")
        payload = callback.get("payload")
        if timestamp is not None and uid is not None and payload is not None:
            return f"callback:{uid}:{timestamp}:{payload}"
        return None
    if update.get("update_type") == "message_created":
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        value = body.get("mid") or message.get("mid") or message.get("message_id")
        return "message:" + str(value) if value else None
    return None


def _update_actor_and_time(update: dict) -> tuple[str | None, int | None]:
    update_type = update.get("update_type")
    timestamp = update.get("timestamp")
    actor = {}
    if update_type == "message_callback":
        callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
        actor = callback.get("user") if isinstance(callback.get("user"), dict) else {}
        timestamp = callback.get("timestamp") or timestamp
    elif update_type == "message_created":
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        actor = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    elif update_type == "bot_started":
        actor = update.get("user") if isinstance(update.get("user"), dict) else {}
    uid = actor.get("user_id")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp = None
    return (str(uid) if uid is not None else None, timestamp)


def _process_update(update: dict, data: dict) -> None:
    update_type = update.get("update_type")
    if update_type == "bot_started":
        user = update.get("user") or {}
        uid = user.get("user_id")
        if uid is not None:
            start(uid, data)
        return
    if update_type == "message_callback":
        callback = update.get("callback") or {}
        actor = callback.get("user") or (update.get("message") or {}).get("sender") or {}
        uid = actor.get("user_id")
        # Callback schema MAX: updates[i].callback.user.user_id + callback.payload.
        callback_id = callback.get("callback_id")
        if callback_id:
            CALLBACK_POOL.submit(answer_callback, callback_id)
        if uid is not None:
            handle_callback(uid, callback.get("payload", ""), data)
        return
    if update_type == "message_created":
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        uid = sender.get("user_id")
        if uid is not None:
            body = message.get("body") if isinstance(message.get("body"), dict) else {}
            text = body.get("text") or message.get("text") or ""
            if text or _extract_file_attachments(message):
                handle_text(uid, text, data, message)
        return


def run_bot() -> None:
    if not BOT_TOKEN:
        print("ОШИБКА: BOT_TOKEN отсутствует в .env")
        return
    marker = load_data().get("max_update_marker")
    ai_state = "настроен (сетевой ответ проверяется при первом запросе)" if AI_CONFIGURED else "не настроен — дневник сохранится без автоматической классификации"
    if not SECURITY_ENABLED:
        security_state = "ВНИМАНИЕ: защита отключена (SECURITY_ENABLED=false)"
    elif TESTING_AUTH_MODE:
        security_state = "ВНИМАНИЕ: тестовый режим — сотрудникам не нужны коды (TESTING_AUTH_MODE=true)"
    else:
        security_state = "авторизация сотрудников по одноразовым кодам включена"
    print(f"ПрактикБот запущен. Сценарии: {FLOW_FILE.name}; YandexGPT: {ai_state}; Безопасность: {security_state}. Ожидание MAX...")
    while True:
        try:
            # Результаты ИИ выдаём до следующего ожидания обновлений.
            process_ai_results()
            # Короткое ожидание помогает быстро показать результат обработки дневника.
            # MAX Long Polling мгновенно отдаёт событие при его появлении; большой timeout
            # не задерживает ответ, а уменьшает число пустых запросов к API.
            has_ai_pending = any(value in {"diary_ai_pending", "assistant_ai_pending"} for value in load_data().get("states", {}).values())
            params: dict[str, Any] = {"timeout": 5 if has_ai_pending else 25,
                                      "types": "bot_started,message_created,message_callback"}
            if marker is not None:
                params["marker"] = marker
            response = HTTP.get(f"{API_URL}/updates", headers=HEADERS, params=params, timeout=(3, 35))
            if response.status_code != 200:
                _http_error("MAX updates", response)
                time.sleep(2)
                process_ai_results()
                continue
            result = response.json()
            next_marker = result.get("marker", marker)
            updates = result.get("updates", [])
            data = load_data()
            processed = data.setdefault("processed_updates", [])
            batch_failed = False
            for update in updates:
                try:
                    fingerprint = _update_fingerprint(update)
                    if fingerprint and fingerprint in processed:
                        continue
                    actor_id, event_time = _update_actor_and_time(update)
                    last_times = data.setdefault("last_user_update_ts", {})
                    if actor_id and event_time is not None:
                        previous_time = last_times.get(actor_id)
                        if previous_time is not None and event_time < int(previous_time):
                            if fingerprint:
                                processed.append(fingerprint)
                            save_data(data)
                            continue
                    _process_update(update, data)
                    if actor_id and event_time is not None:
                        last_times[actor_id] = event_time
                    if fingerprint:
                        processed.append(fingerprint)
                        if len(processed) > 1000:
                            del processed[:-1000]
                    save_data(data)
                except Exception as exc:
                    batch_failed = True
                    print(f"Ошибка обработки обновления {update.get('update_type')}: {exc}")
                    traceback.print_exc()
            # Сохраняем marker только после успешной обработки всего пакета.
            # При ошибке пакет будет получен снова; уже выполненные действия отсеются по fingerprint.
            if not batch_failed:
                marker = next_marker
                data["max_update_marker"] = marker
                save_data(data)
            process_ai_results()
        except KeyboardInterrupt:
            print("Бот остановлен.")
            break
        except (requests.RequestException, ValueError) as exc:
            print(f"Сеть/API MAX: {exc}")
            time.sleep(2)
        except Exception as exc:
            print(f"Критическая ошибка цикла: {exc}")
            traceback.print_exc()
            time.sleep(2)


if __name__ == "__main__":
    run_bot()
