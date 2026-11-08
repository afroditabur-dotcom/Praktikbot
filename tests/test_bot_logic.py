import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FLOW_FILE", str(ROOT / "report_structure.json"))
# Existing behavior tests use fixtures that impersonate registered staff; security tests
# below exercise production authorization with the bypass switched off.
os.environ["TESTING_AUTH_MODE"] = "true"

import main as bot
import ai_assistant as ai


class ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value

    def add_done_callback(self, callback):
        callback(self)


class ImmediatePool:
    def submit(self, fn, *args):
        return ImmediateFuture(fn(*args))


class BotLogicTests(unittest.TestCase):
    def setUp(self):
        self.data = bot._empty_data()
        self.student_code = "ST-A1B2C3"
        self.student_uid = "101"
        self.data["students"][self.student_code] = {
            "id": self.student_code,
            "max_user_id": self.student_uid,
            "user_id": self.student_uid,
            "group_name": "ЭЛС-21",
            "full_name": "Иванов Иван Иванович",
            "invite_code": self.student_code,
        }
        self.data["users"][self.student_uid] = {
            "role": "student", "invite_code": self.student_code
        }
        for p in bot.PRACTICES:
            bot.report_for(self.data, self.student_code, p)

    def add_staff(self, uid="202", role="teacher"):
        self.data[role + "s"][uid] = {"max_user_id": uid, "fio": "Петров Пётр Петрович"}
        self.data["users"][uid] = {"role": role, "full_name": "Петров Пётр Петрович"}
        bot.add_link(uid, role, self.student_code, self.data)
        return uid

    def test_send_message_sets_max_html_format_flag(self):
        response = SimpleNamespace(status_code=201)
        with patch.object(bot.HTTP, "post", return_value=response) as post, \
             patch.object(bot, "_throttle_user"):
            sent = bot.send_message("101", "<b>Панель администратора</b>", text_format="html")
        self.assertTrue(sent)
        self.assertEqual(post.call_args.kwargs["json"]["format"], "html")
        self.assertEqual(post.call_args.kwargs["json"]["text"], "<b>Панель администратора</b>")

    def test_flow_screen_uses_html_and_escapes_dynamic_values(self):
        flow = {"SCREEN": {"message": "<b>Значение:</b> {value}", "keyboard": []}}
        with patch.object(bot, "send_message") as send:
            bot.flow_screen("101", flow, "SCREEN", {"value": "<script>опасно</script>"})
        self.assertEqual(send.call_args.kwargs["text_format"], "html")
        self.assertIn("&lt;script&gt;", send.call_args.args[1])
        self.assertNotIn("<script>", send.call_args.args[1])

    def test_admin_menu_shows_only_approved_tools_and_logout(self):
        with patch.object(bot, "send_message") as send:
            bot.staff_menu("909", "admin")
        call = send.call_args
        buttons = call.args[2]
        labels = [row[0]["text"] for row in buttons]
        payloads = [row[0]["payload"] for row in buttons]
        self.assertEqual(labels, [
            "📥 Экспорт в Excel", "👥 Управление группами",
            "📊 Полная статистика", "📨 Заявки студентов",
            "🤖 ИИ-помощник", "🚪 Выйти из профиля",
        ])
        self.assertEqual(payloads, [
            "ADMIN_EXPORT_XLSX", "GROUP_MGMT", "ADMIN_STATS", "STAFF_REQUESTS", "ASK_AI", "LOGOUT",
        ])
        self.assertEqual(call.kwargs["text_format"], "html")
        self.assertIn("<b>Панель администратора</b>", call.args[1])

    def test_admin_old_menu_actions_are_blocked(self):
        uid = "909"
        self.data["admins"][uid] = {"max_user_id": uid}
        self.data["users"][uid] = {"role": "admin", "full_name": "Администратор Тест Тестович"}
        with patch.object(bot, "send_message") as send, patch.object(bot, "save_data"):
            bot.handle_callback(uid, "ADMIN_ADD_STUDENT", self.data)
        self.assertNotEqual(self.data["states"].get(uid), "link_student")
        self.assertIn("недоступен", send.call_args.args[1])

    def test_report_flow_messages_use_html_not_markdown_bold(self):
        config = json.loads((ROOT / "report_structure.json").read_text(encoding="utf-8"))
        messages = []
        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get("message"), str):
                    messages.append(value["message"])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(config)
        self.assertTrue(any("<b>" in message for message in messages))
        self.assertTrue(all("**" not in message for message in messages))

    def test_fio_validation_and_normalization(self):
        self.assertTrue(bot.valid_fio("иванов   иван иванович"))
        self.assertTrue(bot.valid_fio("Петрова Анна-Мария Сергеевна"))
        self.assertFalse(bot.valid_fio("Ivanov Ivan Ivanovich"))
        self.assertFalse(bot.valid_fio("Иванов Иван"))
        self.assertEqual(bot.normalize_fio(" иВАНОВ   иВАН иВАНОВИЧ "), "Иванов Иван Иванович")

    def test_one_role_per_max_user(self):
        self.assertEqual(bot.role_of(self.student_uid, self.data), "student")
        with patch.object(bot, "send_message") as send:
            bot.handle_callback(self.student_uid, "ROLE:teacher", self.data)
        send.assert_called_once()
        self.assertEqual(bot.role_of(self.student_uid, self.data), "student")

    def test_practices_unlock_sequentially(self):
        self.assertTrue(bot.practice_is_open(self.data, self.student_code, "PM_01"))
        self.assertFalse(bot.practice_is_open(self.data, self.student_code, "PM_02"))
        bot.report_for(self.data, self.student_code, "PM_01")["teacher_grade"] = "4"
        self.assertTrue(bot.practice_is_open(self.data, self.student_code, "PM_02"))
        self.assertFalse(bot.practice_is_open(self.data, self.student_code, "PM_03"))
        bot.report_for(self.data, self.student_code, "PM_02")["teacher_grade"] = "Зачтено"
        self.assertTrue(bot.practice_is_open(self.data, self.student_code, "PM_03"))
        self.assertFalse(bot.practice_is_open(self.data, self.student_code, "PM_09"))

    def test_callback_translation_for_current_json(self):
        cases = {
            "ROLE_STUDENT": "ROLE:student",
            "ROLE_TEACHER": "ROLE:teacher",
            "ROLE_EMPLOYER": "ROLE:employer",
            "ROLE_ADMIN": "ROLE:admin",
            "AUTH_GROUP:ЭЛС-21": "GROUP:ЭЛС-21",
            "AUTH_CONFIRM_YES": "REG_CONFIRM",
            "PRACTICE_OPEN:PM_01": "PRACTICE:PM_01",
            "REPORT_SECTION:PM_01:1": "SECTION:PM_01:1",
            "REPORT_INPUT:PM_01:1": "SECTION_SAVE:PM_01:1",
            "SUBMIT_REPORT:PM_01": "SUBMIT:PM_01",
            "DIARY_ADD_DATE:PM_03": "DIARY_ADD:PM_03",
            "DIARY_VIEW_ENTRIES:PM_03:1": "DIARY_VIEW:PM_03:0",
            "TEACHER_NEED_CHECK": "TEACHER_FILTER:submitted",
            "TEACHER_ALREADY_CHECKED": "TEACHER_FILTER:graded",
            f"TEACHER_ADD_COMMENT:{self.student_code}:PM_01": f"TEACHER_COMMENT:{self.student_code}:PM_01",
            f"TEACHER_ADD_GRADE:{self.student_code}:PM_01": f"GRADE:{self.student_code}:PM_01",
            f"EMPLOYER_APPROVE_RECORD:{self.student_code}:PM_03": f"APPROVE:{self.student_code}:PM_03",
            f"EMPLOYER_ADD_NOTE:{self.student_code}:PM_03": f"EMPLOYER_NOTE:{self.student_code}:PM_03",
        }
        for payload, expected in cases.items():
            with self.subTest(payload=payload):
                self.assertEqual(bot.normalize_payload(self.student_uid, payload, self.data), expected)

    def test_max_file_attachment_extraction_fields(self):
        message = {"body": {"attachments": [{
            "type": "file",
            "payload": {"token": "MAX-TOKEN-123", "url": "https://cdn.example/doc.pdf",
                         "filename": "practice.pdf", "size": 12345},
        }]}}
        result = bot._extract_file_attachments(message)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["token"], "MAX-TOKEN-123")
        self.assertEqual(result[0]["url"], "https://cdn.example/doc.pdf")
        self.assertEqual(result[0]["name"], "practice.pdf")
        self.assertEqual(result[0]["size"], 12345)

    def test_student_document_saved_with_token_and_url(self):
        self.data["states"][self.student_uid] = "section_text"
        self.data["temporary"][self.student_uid] = {"practice": "PM_01", "section": "1"}
        message = {"body": {"attachments": [{
            "type": "file", "payload": {"token": "F-TOKEN", "url": "https://cdn.example/x.docx",
                                            "filename": "report.docx", "size": 222},
        }]}}
        with patch.object(bot, "save_data"), patch.object(bot, "send_message"), patch.object(bot, "practice_screen"):
            bot.handle_text(self.student_uid, "", self.data, message)
        report = bot.report_for(self.data, self.student_code, "PM_01")
        self.assertEqual(report["files"]["1"][0]["token"], "F-TOKEN")
        self.assertEqual(report["files"]["1"][0]["url"], "https://cdn.example/x.docx")
        self.assertEqual(report["section_1_file_name"], "report.docx")

    def test_section_three_rejects_document(self):
        self.data["states"][self.student_uid] = "section_text"
        self.data["temporary"][self.student_uid] = {"practice": "PM_01", "section": "3"}
        message = {"body": {"attachments": [{"type": "file", "payload": {"token": "T"}}]}}
        with patch.object(bot, "send_message") as send:
            bot.handle_text(self.student_uid, "", self.data, message)
        self.assertFalse(bot.report_for(self.data, self.student_code, "PM_01")["files"].get("3"))
        self.assertIn("документ прикреплять нельзя", send.call_args.args[1])

    def test_incomplete_report_cannot_be_submitted(self):
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_callback(self.student_uid, "SUBMIT:PM_01", self.data)
        self.assertEqual(bot.report_for(self.data, self.student_code, "PM_01")["status"], "draft")

    def test_complete_report_can_be_submitted_and_teacher_notified(self):
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["sections"] = {"1": "a", "2": "b", "3": "c", "4": "d"}
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"), patch.object(bot, "notify_linked") as notify, patch.object(bot, "student_menu"):
            bot.handle_callback(self.student_uid, "SUBMIT:PM_01", self.data)
        self.assertEqual(report["status"], "submitted")
        notify.assert_called_once()

    def test_student_cannot_edit_submitted_report(self):
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["status"] = "submitted"
        self.data["states"][self.student_uid] = "section_text"
        self.data["temporary"][self.student_uid] = {"practice": "PM_01", "section": "1"}
        with patch.object(bot, "send_message") as send:
            bot.handle_text(self.student_uid, "новый текст", self.data)
        self.assertNotEqual(report["sections"].get("1"), "новый текст")
        self.assertIn("Нельзя изменить", send.call_args.args[1])

    def test_teacher_gets_actual_file_attachment_on_practice_view(self):
        uid = self.add_staff("202", "teacher")
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["files"]["1"] = [{"type": "file", "token": "TOKEN-1", "url": "https://files/x", "name": "work.pdf"}]
        with patch.object(bot, "send_message"), patch.object(bot, "send_file_attachment") as send_file, patch.object(bot, "save_data"):
            bot._staff_student_view(uid, self.student_code, "teacher", self.data, "PM_01")
        send_file.assert_called_once()
        self.assertEqual(send_file.call_args.args[1]["token"], "TOKEN-1")

    def test_teacher_grade_unlocks_next_practice_and_records_audit_fields(self):
        uid = self.add_staff("202", "teacher")
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["status"] = "submitted"
        self.data["users"][uid]["selected_student_code"] = self.student_code
        self.data["users"][uid]["current_practice"] = "PM_01"
        self.data["states"][uid] = "grade"
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"), patch.object(bot, "notify_student"):
            bot.handle_text(uid, "5", self.data)
        self.assertEqual(report["teacher_grade"], "5")
        self.assertEqual(report["status"], "graded")
        self.assertEqual(report["graded_by_teacher_id"], uid)
        self.assertIsNotNone(report.get("graded_at"))
        self.assertTrue(bot.practice_is_open(self.data, self.student_code, "PM_02"))

    def test_teacher_cannot_grade_unlinked_student(self):
        uid = "303"
        self.data["teachers"][uid] = {"max_user_id": uid}
        self.data["users"][uid] = {"role": "teacher", "selected_student_code": self.student_code, "current_practice": "PM_01"}
        self.data["states"][uid] = "grade"
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["status"] = "submitted"
        with patch.object(bot, "send_message") as send:
            bot.handle_text(uid, "5", self.data)
        self.assertIsNone(report["teacher_grade"])
        self.assertIn("Нет доступа", send.call_args.args[1])

    def test_employer_note_is_visible_to_student(self):
        uid = self.add_staff("404", "employer")
        self.data["users"][uid]["selected_student_code"] = self.student_code
        self.data["users"][uid]["current_practice"] = "PM_01"
        self.data["states"][uid] = "employer_note"
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"), patch.object(bot, "notify_student"):
            bot.handle_text(uid, "Пожалуйста, уточните описание выполненной работы.", self.data)
        report = bot.report_for(self.data, self.student_code, "PM_01")
        self.assertIn("уточните", report["employer_note"])
        self.assertTrue(any("Комментарий наставника" in line for line in bot.feedback_lines(report)))

    def test_diary_ai_confirmation_saves_pc_and_formal_text(self):
        self.data["users"][self.student_uid]["current_practice"] = "PM_03"
        bot.report_for(self.data, self.student_code, "PM_02")["teacher_grade"] = "4"
        self.data["states"][self.student_uid] = "diary_comment"
        self.data["temporary"][self.student_uid] = {"practice": "PM_03", "entry_date": "04.11.2026"}
        with patch.object(bot, "analyze_work_description", return_value={"pc_code": "ПК 3.2", "formal_text": "Официальная запись."}), \
             patch.object(bot, "AI_POOL", ImmediatePool()), \
             patch.object(bot, "load_data", return_value=self.data), \
             patch.object(bot, "save_data"), patch.object(bot, "send_message"):
            bot.handle_text(self.student_uid, "протирал шкафы от пыли", self.data)
            bot.process_ai_results()
        self.assertEqual(self.data["states"][self.student_uid], "diary_confirm")
        self.assertEqual(self.data["temporary"][self.student_uid]["pc_code"], "ПК 3.2")
        with patch.object(bot, "save_data"), patch.object(bot, "practice_screen"), patch.object(bot, "send_message"):
            bot.handle_callback(self.student_uid, "DIARY_SAVE", self.data)
        entries = bot.diary_for(self.data, self.student_code, "PM_03")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pc_code"], "ПК 3.2")
        self.assertEqual(entries[0]["comment"], "Официальная запись.")

    def test_ai_rejects_competency_not_in_practice_catalog(self):
        class FakeClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"pc_code":"ПК 99.9","formal_text":"Текст"}'))])
        settings = ai.Settings(
            api_key="test-key", folder_id="test-folder",
            base_url="https://example.invalid/v1", model="gpt://test-folder/test-model",
            timeout=1, max_retries=0,
            competencies_file=ROOT / "pm_competencies.json",
        )
        with patch.object(ai, "_get_client", return_value=FakeClient()), \
             patch.object(ai, "get_settings", return_value=settings), \
             patch.object(ai, "_load_competencies", return_value={"PM_03": {"competencies": [{"code": "ПК 3.2"}]}}):
            result = ai.analyze_work_description("протирал шкафы", "PM_03")
        self.assertIsNone(result["pc_code"])
        self.assertEqual(result["formal_text"], "Текст")

    def test_future_date_is_rejected(self):
        from datetime import date, timedelta
        future = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
        self.data["states"][self.student_uid] = "diary_date"
        self.data["temporary"][self.student_uid] = {"practice": "PM_03"}
        with patch.object(bot, "send_message") as send:
            bot.handle_text(self.student_uid, future, self.data)
        self.assertEqual(self.data["states"][self.student_uid], "diary_date")
        self.assertIn("будущем", send.call_args.args[1])


    def test_student_registration_end_to_end(self):
        uid = "505"
        data = bot._empty_data()
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"), patch.object(bot, "student_menu") as menu:
            bot.handle_callback(uid, "ROLE:student", data)
            self.assertEqual(data["states"][uid], "student_group")
            bot.handle_callback(uid, "GROUP:ЭЛС-21", data)
            self.assertEqual(data["states"][uid], "student_fio")
            bot.handle_text(uid, "  иВАНОВ   иВАН иВАНОВИЧ ", data)
            self.assertEqual(data["states"][uid], "registration_confirm")
            self.assertEqual(data["temporary"][uid]["fio"], "Иванов Иван Иванович")
            bot.handle_callback(uid, "AUTH_CONFIRM_YES", data)
        self.assertEqual(bot.role_of(uid, data), "student")
        student_code = bot.student_code(uid, data)
        self.assertRegex(student_code, r"^ST-[A-F0-9]{6}$")
        self.assertEqual(data["students"][student_code]["group_name"], "ЭЛС-21")
        self.assertEqual(len(data["practice_reports"][student_code]), 8)
        menu.assert_called_once()

    def test_invalid_staff_practice_code_is_rejected(self):
        uid = self.add_staff("606", "teacher")
        self.data["users"][uid]["selected_student_code"] = self.student_code
        self.data["users"][uid]["current_practice"] = "PM_99"
        self.data["states"][uid] = "grade"
        with patch.object(bot, "send_message") as send:
            bot.handle_text(uid, "5", self.data)
        self.assertNotIn("PM_99", self.data["practice_reports"].get(self.student_code, {}))
        self.assertIn("Нет доступа", send.call_args.args[1])

    def test_employer_cannot_add_note_to_unknown_practice(self):
        uid = self.add_staff("707", "employer")
        with patch.object(bot, "send_message") as send:
            bot.handle_callback(uid, f"EMPLOYER_NOTE:{self.student_code}:PM_99", self.data)
        self.assertNotIn("PM_99", self.data["practice_reports"].get(self.student_code, {}))
        self.assertIn("Нет доступа", send.call_args.args[1])

    def test_diary_repeated_date_updates_instead_of_duplicating(self):
        self.data["users"][self.student_uid]["current_practice"] = "PM_03"
        bot.report_for(self.data, self.student_code, "PM_02")["teacher_grade"] = "4"
        entries = bot.diary_for(self.data, self.student_code, "PM_03")
        entries.append({"entry_date": "04.11.2026", "comment": "Старая запись", "pc_code": "ПК 3.1"})
        self.data["states"][self.student_uid] = "diary_confirm"
        self.data["temporary"][self.student_uid] = {
            "practice": "PM_03", "entry_date": "04.11.2026",
            "formatted_comment": "Новая запись", "original_comment": "делал работу", "pc_code": "ПК 3.2"
        }
        with patch.object(bot, "save_data"), patch.object(bot, "practice_screen"):
            bot.handle_callback(self.student_uid, "DIARY_SAVE", self.data)
        entries = bot.diary_for(self.data, self.student_code, "PM_03")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["comment"], "Новая запись")
        self.assertEqual(entries[0]["pc_code"], "ПК 3.2")

    def test_file_forwarding_uses_received_max_token(self):
        response = SimpleNamespace(status_code=201, text="")
        info = {"type": "file", "token": "MAX-TOKEN-456", "name": "акт.docx"}
        with patch.object(bot, "_throttle_user"), patch.object(bot.HTTP, "post", return_value=response) as post:
            self.assertTrue(bot.send_file_attachment("808", info, "ПМ.01, раздел 1"))
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["params"], {"user_id": 808})
        self.assertEqual(kwargs["json"]["attachments"], [{"type": "file", "payload": {"token": "MAX-TOKEN-456"}}])
        self.assertIn("акт.docx", kwargs["json"]["text"])

    def test_json_data_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot_data.json"
            with patch.object(bot, "DATA_FILE", path):
                bot.save_data(self.data)
                loaded = bot.load_data()
            self.assertEqual(loaded["students"][self.student_code]["group_name"], "ЭЛС-21")
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name("bot_data.json.tmp").exists())

    def test_report_flow_config_matches_eight_practices(self):
        flow = json.loads((ROOT / "report_structure.json").read_text(encoding="utf-8"))
        catalog = flow["backend_logic_requirements"]["practice_catalog"]
        self.assertEqual([x["code"] for x in catalog], bot.PRACTICES)
        expected_types = {"PM_01": "report", "PM_02": "report", "PM_03": "diary",
                          "PM_04": "report", "PM_05": "report", "PM_06": "diary",
                          "PM_07": "diary", "PM_08": "diary"}
        self.assertEqual({x["code"]: x["type"] for x in catalog}, expected_types)
        sections = flow["backend_logic_requirements"]["report_sections"]["PM_01"]
        self.assertEqual([x["number"] for x in sections], [1, 2, 3, 4])
        self.assertEqual(sections[2]["allowed_input"], "text")

    def test_competencies_json_valid_and_pm08_gap_is_explicit(self):
        path = ROOT / "pm_competencies.json"
        competencies = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue({"PM_03", "PM_06", "PM_07"}.issubset(competencies))
        self.assertNotIn("PM_08", competencies)
        for practice, module in competencies.items():
            codes = [x["code"] for x in module["competencies"]]
            self.assertTrue(codes, practice)
            self.assertEqual(len(codes), len(set(codes)), practice)



    def test_grade_value_validation(self):
        uid = self.add_staff("909", "teacher")
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["status"] = "submitted"
        self.data["users"][uid]["selected_student_code"] = self.student_code
        self.data["users"][uid]["current_practice"] = "PM_01"
        self.data["states"][uid] = "grade"
        with patch.object(bot, "send_message") as send:
            bot.handle_text(uid, "2", self.data)
        self.assertEqual(report["status"], "submitted")
        self.assertIsNone(report["teacher_grade"])
        self.assertIn("Допустимая оценка", send.call_args.args[1])

    def test_student_can_read_teacher_and_employer_feedback(self):
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report.update({"status": "graded", "teacher_grade": "5",
                       "teacher_comment": "Отчёт оформлен правильно.",
                       "employer_approved": True,
                       "employer_note": "Практика пройдена успешно."})
        text = bot._render_student_practice(self.student_uid, self.student_code, "PM_01", self.data)
        self.assertIn("Отчёт оформлен правильно", text)
        self.assertIn("Практика пройдена успешно", text)
        self.assertIn("Подтверждение наставника", text)

    def test_student_cannot_open_locked_practice_from_old_button(self):
        with patch.object(bot, "send_message") as send:
            bot.handle_callback(self.student_uid, "PRACTICE_OPEN:PM_03", self.data)
        self.assertIn("пока закрыта", send.call_args.args[1])
        self.assertNotEqual(self.data["users"][self.student_uid].get("current_practice"), "PM_03")

    def test_status_labels_are_russian(self):
        self.assertEqual(bot.status_label("draft"), "Заполняется")
        self.assertEqual(bot.status_label("submitted"), "На проверке")
        self.assertEqual(bot.status_label("graded"), "Оценено")


    def test_ai_question_fallback_is_clear_when_not_configured(self):
        with patch.object(ai, "_get_client", return_value=None):
            answer = ai.answer_student_question("Как оформить титульный лист?")
        self.assertIn("не подключён", answer)

    def test_student_ai_question_returns_async_answer_in_chat(self):
        self.data["states"][self.student_uid] = "assistant_question"
        with patch.object(bot, "answer_student_question", return_value="Ответ помощника."), \
             patch.object(bot, "AI_POOL", ImmediatePool()), \
             patch.object(bot, "send_message") as send, patch.object(bot, "save_data"), \
             patch.object(bot, "load_data", return_value=self.data):
            bot.handle_text(self.student_uid, "Как оформить отчёт?", self.data)
            self.assertEqual(self.data["states"][self.student_uid], "assistant_ai_pending")
            bot.process_ai_results()
        self.assertNotIn(self.student_uid, self.data["states"])
        self.assertTrue(any("Ответ помощника." in call.args[1] for call in send.call_args_list))

    def test_ai_fallback_without_credentials_preserves_student_text(self):
        with patch.object(ai, "_get_client", return_value=None):
            result = ai.analyze_work_description("убирал рабочее место", "PM_03")
        self.assertFalse(result["ai_used"])
        self.assertIsNone(result["pc_code"])
        self.assertEqual(result["formal_text"], "убирал рабочее место")

    def test_corrupt_json_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot_data.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(bot, "DATA_FILE", path):
                with self.assertRaises(RuntimeError):
                    bot.load_data()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")



    def test_diary_date_requires_two_digit_day_and_month(self):
        self.data["users"][self.student_uid]["current_practice"] = "PM_03"
        self.data["states"][self.student_uid] = "diary_date"
        self.data["temporary"][self.student_uid] = {"practice": "PM_03"}
        with patch.object(bot, "send_message") as send:
            bot.handle_text(self.student_uid, "5.11.2026", self.data)
        self.assertEqual(self.data["states"][self.student_uid], "diary_date")
        self.assertIn("ДД.ММ.ГГГГ", send.call_args.args[1])

    def test_legacy_graded_record_with_grade_is_closed(self):
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["status"] = "draft"
        report["teacher_grade"] = "4"
        normalized = bot.report_for(self.data, self.student_code, "PM_01")
        self.assertEqual(normalized["status"], "graded")



    def test_available_groups_includes_active_managed_groups_only(self):
        self.data["managed_groups"] = [
            {"id": "GR-1", "name": "ЭЛС-23", "active": True},
            {"id": "GR-2", "name": "ЭЛС-24", "active": False},
        ]
        groups = bot.available_groups(self.data)
        self.assertIn("ЭЛС-21", groups)
        self.assertIn("ЭЛС-23", groups)
        self.assertNotIn("ЭЛС-24", groups)

    def test_student_can_enter_a_new_group_manually(self):
        self.data["states"][self.student_uid] = "student_group"
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_callback(self.student_uid, "GROUP_OTHER", self.data)
            self.assertEqual(self.data["states"][self.student_uid], "student_group_custom")
            bot.handle_text(self.student_uid, "ЭЛС-25", self.data)
            self.assertEqual(self.data["temporary"][self.student_uid]["group_name"], "ЭЛС-25")
            bot.handle_text(self.student_uid, "Смирнов Сергей Сергеевич", self.data)
            bot.handle_callback(self.student_uid, "REG_CONFIRM", self.data)
        code = bot.student_code(self.student_uid, self.data)
        self.assertEqual(self.data["students"][code]["group_name"], "ЭЛС-25")

    def test_teacher_can_create_and_archive_a_group(self):
        uid = self.add_staff()
        self.data["states"][uid] = "group_create_name"
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_text(uid, "ЭЛС-30", self.data)
            self.assertEqual(self.data["states"][uid], "group_create_specialty")
            bot.handle_text(uid, "Электроснабжение", self.data)
        group = next(x for x in self.data["managed_groups"] if x["name"] == "ЭЛС-30")
        self.assertIn("ЭЛС-30", bot.available_groups(self.data))
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_callback(uid, f"GROUP_TOGGLE:{group['id']}", self.data)
        self.assertFalse(group["active"])
        self.assertNotIn("ЭЛС-30", bot.available_groups(self.data))


    def test_group_rename_updates_student_group_without_losing_grades(self):
        uid = self.add_staff()
        group = {"id": "GR-RENAME", "name": "ЭЛС-21", "specialty": "Электроснабжение",
                 "created_by": uid, "active": True}
        self.data["managed_groups"].append(group)
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["teacher_grade"] = "5"
        self.data["states"][uid] = "group_rename"
        self.data["temporary"][uid] = {"group_id": "GR-RENAME"}
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_text(uid, "ЭЛС-99", self.data)
        self.assertEqual(self.data["students"][self.student_code]["group_name"], "ЭЛС-99")
        self.assertEqual(report["teacher_grade"], "5")
        self.assertEqual(group["name"], "ЭЛС-99")

    def test_teacher_cannot_manage_another_teachers_group(self):
        uid1 = self.add_staff("202", "teacher")
        uid2 = self.add_staff("203", "teacher")
        group = {"id": "GR-OWNED", "name": "ЭЛС-90", "created_by": uid1, "active": True}
        self.data["managed_groups"].append(group)
        with patch.object(bot, "send_message") as send:
            bot.handle_callback(uid2, "GROUP_TOGGLE:GR-OWNED", self.data)
        self.assertTrue(group["active"])
        self.assertIn("Нет доступа", send.call_args.args[1])


    def test_student_menu_shows_customized_practice_title(self):
        self.data["practice_settings"]["PM_01"] = {"title": "Введение в практику"}
        with patch.object(bot, "send_message") as send:
            bot.student_menu(self.student_uid, self.data)
        keyboard = send.call_args.args[2]
        first_button_text = keyboard[0][0]["text"]
        self.assertIn("ПМ.01 — Введение в практику", first_button_text)

    def test_practice_metadata_edit_does_not_change_practice_sequence(self):
        uid = self.add_staff()
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_callback(uid, "PRACTICE_EDIT_TITLE:PM_01", self.data)
            bot.handle_text(uid, "Введение в практику", self.data)
        self.assertEqual(bot.practice_label("PM_01", self.data), "ПМ.01 — Введение в практику")
        self.assertEqual(bot.PRACTICES, [f"PM_{i:02d}" for i in range(1, 9)])
        self.assertTrue(bot.practice_is_open(self.data, self.student_code, "PM_01"))
        self.assertFalse(bot.practice_is_open(self.data, self.student_code, "PM_02"))

    def test_custom_practice_is_saved_as_catalog_only(self):
        uid = self.add_staff()
        self.data["states"][uid] = "custom_practice_title"
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_text(uid, "Дополнительная практика", self.data)
            bot.handle_text(uid, "Описание для новой практики", self.data)
            bot.handle_text(uid, "производственная", self.data)
        item = self.data["custom_practices"][0]
        self.assertEqual(item["title"], "Дополнительная практика")
        self.assertFalse(item["workflow_enabled"])
        self.assertNotIn(item["id"], bot.PRACTICES)

    def test_custom_report_section_is_saved_as_scaffold_not_required(self):
        uid = self.add_staff()
        self.data["states"][uid] = "custom_section_title"
        self.data["temporary"][uid] = {
            "custom_section_group": "ЭЛС-21",
            "custom_section_practice": "PM_01",
        }
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_text(uid, "Анализ схемы собственных нужд", self.data)
            bot.handle_text(uid, "Опишите назначение и состав схемы.", self.data)
        section = self.data["custom_sections"][0]
        self.assertEqual(section["group_name"], "ЭЛС-21")
        self.assertFalse(section["workflow_enabled"])
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["sections"] = {"1": "a", "2": "b", "3": "c", "4": "d"}
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"), patch.object(bot, "notify_linked"), patch.object(bot, "student_menu"):
            bot.handle_callback(self.student_uid, "SUBMIT:PM_01", self.data)
        self.assertEqual(report["status"], "submitted")


    def test_archived_custom_sections_remain_visible_for_restore(self):
        self.data["custom_sections"].append({"id": "CS-1", "group_name": "ЭЛС-21", "pm_code": "PM_01",
                                              "section_title": "Доп. раздел", "active": False, "teacher_id": "202"})
        values = bot.custom_sections_for_group(self.data, "ЭЛС-21", "PM_01")
        self.assertEqual(len(values), 1)
        self.assertFalse(values[0]["active"])

    def test_teacher_can_edit_custom_section_title_without_enabling_workflow(self):
        uid = self.add_staff()
        section = {"id": "CS-EDIT", "group_name": "ЭЛС-21", "pm_code": "PM_01",
                   "section_title": "Старое название", "section_hint": "Старая подсказка",
                   "teacher_id": uid, "active": True, "workflow_enabled": False}
        self.data["custom_sections"].append(section)
        with patch.object(bot, "send_message"), patch.object(bot, "save_data"):
            bot.handle_callback(uid, "CUSTOM_SECTION_EDIT_TITLE:CS-EDIT", self.data)
            bot.handle_text(uid, "Новое название", self.data)
        self.assertEqual(section["section_title"], "Новое название")
        self.assertFalse(section["workflow_enabled"])
        self.assertEqual(self.data["states"].get(uid), None)

    def test_student_request_creation_is_disabled_and_staff_can_answer_legacy_request(self):
        teacher_uid = self.add_staff("202", "teacher")
        self.data["states"][self.student_uid] = "request_description"
        self.data["temporary"][self.student_uid] = {"request_kind": "Консультация"}
        with patch.object(bot, "send_message") as send, patch.object(bot, "save_data"):
            bot.handle_text(self.student_uid, "Нужна консультация по оформлению дневника.", self.data)
        self.assertEqual(self.data["requests"], [])
        self.assertNotIn(self.student_uid, self.data["states"])
        self.assertIn("отключено", send.call_args.args[1])

        # Existing requests remain available for staff processing after the student flow was disabled.
        item = {
            "id": "REQ-OLD-1", "student_code": self.student_code,
            "kind": "Консультация", "description": "Нужна консультация.",
            "status": "новая", "response": "",
        }
        self.data["requests"].append(item)
        self.data["states"][teacher_uid] = "request_response"
        self.data["temporary"][teacher_uid] = {"request_id": item["id"]}
        with patch.object(bot, "send_message") as send, patch.object(bot, "save_data"):
            bot.handle_text(teacher_uid, "Подойдите после занятия, разберём оформление.", self.data)
        self.assertEqual(item["status"], "ответ дан")
        self.assertIn("разберём оформление", item["response"])
        self.assertTrue(any("получен ответ" in call.args[1] for call in send.call_args_list))

    def test_teacher_reminder_goes_only_to_linked_students_with_unfinished_diary(self):
        teacher_uid = self.add_staff()
        with patch.object(bot, "send_message") as send:
            bot.handle_callback(teacher_uid, "REMIND:PM_03", self.data)
        targets = [str(call.args[0]) for call in send.call_args_list]
        self.assertIn(self.student_uid, targets)
        self.assertIn(teacher_uid, targets)

    def test_report_export_text_includes_student_metadata_and_sections(self):
        report = bot.report_for(self.data, self.student_code, "PM_01")
        report["sections"] = {"1": "Описание предприятия", "2": "Обязанности", "3": "Охрана труда", "4": "Итоги"}
        content = bot._report_text_for_export(self.student_code, "PM_01", self.data)
        self.assertIn("Иванов Иван Иванович", content)
        self.assertIn("ЭЛС-21", content)
        self.assertIn("Описание предприятия", content)

    def test_admin_xlsx_export_generates_workbook_payload(self):
        uid = "909"
        self.data["admins"][uid] = {"max_user_id": uid}
        self.data["users"][uid] = {"role": "admin"}
        with patch.object(bot, "_send_generated_file") as send_file:
            bot._export_admin_xlsx(uid, self.data)
        send_file.assert_called_once()
        filename, content = send_file.call_args.args[1:3]
        self.assertTrue(filename.endswith(".xlsx"))
        self.assertGreater(len(content), 1000)

    def test_generated_file_upload_uses_max_upload_then_message(self):
        uid = "101"
        upload_init = SimpleNamespace(status_code=200, json=lambda: {"url": "https://upload.example/file"})
        upload_done = SimpleNamespace(status_code=200, content=b'{"token":"FILE-TOKEN"}', json=lambda: {"token": "FILE-TOKEN"})
        sent = SimpleNamespace(status_code=201)
        with patch.object(bot, "BOT_TOKEN", "test-token"), \
             patch.object(bot.HTTP, "post", side_effect=[upload_init, upload_done, sent]) as post, \
             patch.object(bot.time, "sleep", lambda _: None):
            ok = bot._send_generated_file(uid, "test.txt", b"hello", "Файл")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args.args[0], f"{bot.API_URL}/messages")
        self.assertEqual(post.call_args.kwargs["json"]["attachments"][0]["payload"]["token"], "FILE-TOKEN")

    def test_generated_file_retries_when_attachment_is_not_ready(self):
        uid = "101"
        upload_init = SimpleNamespace(status_code=200, json=lambda: {"url": "https://upload.example/file"})
        upload_done = SimpleNamespace(status_code=200, content=b'{"token":"FILE-TOKEN"}', json=lambda: {"token": "FILE-TOKEN"})
        not_ready = SimpleNamespace(status_code=400, json=lambda: {"code": "attachment.not.ready"})
        sent = SimpleNamespace(status_code=201)
        with patch.object(bot, "BOT_TOKEN", "test-token"), \
             patch.object(bot.HTTP, "post", side_effect=[upload_init, upload_done, not_ready, sent]) as post, \
             patch.object(bot.time, "sleep", lambda _: None):
            ok = bot._send_generated_file(uid, "test.txt", b"hello", "Файл")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 4)



if __name__ == "__main__":
    unittest.main(verbosity=2)
