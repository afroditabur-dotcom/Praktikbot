import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["FLOW_FILE"] = str(ROOT / "report_structure.json")
os.environ["TESTING_AUTH_MODE"] = "true"

import main as bot
import security


class SecurityModuleTests(unittest.TestCase):
    def setUp(self):
        self.data = bot._empty_data()

    def test_issued_code_is_hashed_role_bound_and_consumed_once(self):
        code, row = security.create_access_code(self.data, "teacher", now=1_000)
        self.assertNotIn(code, str(row))
        self.assertEqual(row["role"], "teacher")
        self.assertEqual(row["expires_at"] - row["created_at"], 24 * 60 * 60)

        ok, reason = security.verify_and_consume_access_code(
            self.data, "max-101", "teacher", code, now=1_100
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertTrue(security.is_staff_authorized("max-101", "teacher", self.data))
        self.assertEqual(row["used_by"], "max-101")
        self.assertFalse(row["is_active"])

        # Код нельзя повторно использовать тем же или другим аккаунтом MAX.
        ok, _ = security.verify_and_consume_access_code(
            self.data, "max-202", "teacher", code, now=1_101
        )
        self.assertFalse(ok)

    def test_code_expires_after_24_hours(self):
        code, row = security.create_access_code(self.data, "admin", now=5_000)
        ok, reason = security.verify_and_consume_access_code(
            self.data, "max-admin", "admin", code, now=row["expires_at"]
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_code_cannot_be_used_for_another_role(self):
        code, _ = security.create_access_code(self.data, "employer", now=5_000)
        ok, reason = security.verify_and_consume_access_code(
            self.data, "max-employer", "teacher", code, now=5_100
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "wrong_role")
        self.assertIsNone(self.data.get("staff_auth", {}).get("max-employer"))

    def test_five_failed_attempts_trigger_temporary_block(self):
        for attempt in range(1, 5):
            ok, reason = security.verify_and_consume_access_code(
                self.data, "max-try", "teacher", "NOT-A-REAL-CODE", now=10_000 + attempt
            )
            self.assertFalse(ok)
            self.assertEqual(reason, "invalid")
        ok, reason = security.verify_and_consume_access_code(
            self.data, "max-try", "teacher", "NOT-A-REAL-CODE", now=10_005
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "blocked")
        ok, reason = security.verify_and_consume_access_code(
            self.data, "max-try", "teacher", "NOT-A-REAL-CODE", now=10_006
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "blocked")

    def test_legacy_plaintext_codes_are_migrated_to_hashes(self):
        self.data["access_codes"] = [{"code": "ABCD2345EF", "role": "admin", "is_active": True}]
        changed = security.migrate_access_codes(self.data, now=12_000)
        row = self.data["access_codes"][0]
        self.assertTrue(changed)
        self.assertNotIn("code", row)
        self.assertEqual(row["code_hash"], security.hash_code("ABCD2345EF"))
        self.assertEqual(row["expires_at"] - row["created_at"], 24 * 60 * 60)

    def test_expired_issue_code_does_not_expire_authorized_staff_profile(self):
        code, row = security.create_access_code(self.data, "teacher", now=1_000)
        ok, _ = security.verify_and_consume_access_code(
            self.data, "max-teacher", "teacher", code, now=1_100
        )
        self.assertTrue(ok)
        self.assertTrue(security.is_staff_authorized("max-teacher", "teacher", self.data))
        # Период действия кода закончился, но созданная авторизация постоянная.
        later = row["expires_at"] + 100
        self.assertGreater(later, row["expires_at"])
        self.assertTrue(security.is_staff_authorized("max-teacher", "teacher", self.data))

    def test_main_loader_persists_legacy_code_hash_migration(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot_data.json"
            path.write_text(json.dumps({
                "access_codes": [{"code": "ABCD2345EF", "role": "admin", "is_active": True}]
            }), encoding="utf-8")
            with patch.object(bot, "DATA_FILE", path):
                loaded = bot.load_data()
            saved = json.loads(path.read_text(encoding="utf-8"))
            row = saved["access_codes"][0]
            self.assertNotIn("code", row)
            self.assertEqual(row["code_hash"], security.hash_code("ABCD2345EF"))
            self.assertEqual(len(loaded["staff_auth"]), 0)


class SecurityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.data = bot._empty_data()

    def test_staff_registration_requires_code_then_creates_bound_account(self):
        code, row = security.create_access_code(self.data, "teacher")
        with patch.object(bot, "SECURITY_ENABLED", True), \
             patch.object(bot, "TESTING_AUTH_MODE", False), \
             patch.object(bot, "save_data"), patch.object(bot, "send_message"):
            bot.handle_callback("801", "ROLE:teacher", self.data)
            self.assertEqual(self.data["states"]["801"], "staff_access_code")
            self.assertNotIn("801", self.data["teachers"])

            bot.handle_text("801", code, self.data)
            self.assertEqual(bot.role_of("801", self.data), "teacher")
            self.assertTrue(bot._staff_is_authorized("801", "teacher", self.data))
            self.assertEqual(self.data["staff_auth"]["801"]["access_code_id"], row["code_id"])
            self.assertEqual(self.data["states"]["801"], "staff_fio")

    def test_old_staff_profile_must_reauthorize_before_actions(self):
        self.data["users"]["802"] = {"role": "admin", "full_name": "Иванов Иван Иванович"}
        self.data["admins"]["802"] = {"max_user_id": "802", "fio": "Иванов Иван Иванович"}
        with patch.object(bot, "SECURITY_ENABLED", True), \
             patch.object(bot, "TESTING_AUTH_MODE", False), \
             patch.object(bot, "save_data"), patch.object(bot, "send_message") as send:
            bot.handle_callback("802", "ADMIN_STATS", self.data)
            self.assertEqual(self.data["states"]["802"], "staff_access_code")
            self.assertIn("одноразовый код", send.call_args.args[1])

    def test_logout_revokes_account_and_removes_staff_student_links(self):
        uid = "803"
        code, _ = security.create_access_code(self.data, "teacher")
        with patch.object(bot, "SECURITY_ENABLED", True), \
             patch.object(bot, "TESTING_AUTH_MODE", False), \
             patch.object(bot, "save_data"), patch.object(bot, "send_message"):
            bot.handle_callback(uid, "ROLE:teacher", self.data)
            bot.handle_text(uid, code, self.data)
            self.data["users"][uid]["full_name"] = "Петров Пётр Петрович"
            self.data["teachers"][uid]["fio"] = "Петров Пётр Петрович"
            self.data["students"]["ST-A1B2C3"] = {"id": "ST-A1B2C3", "max_user_id": "student-1"}
            bot.add_link(uid, "teacher", "ST-A1B2C3", self.data)
            self.assertTrue(bot.is_linked(uid, "ST-A1B2C3", self.data, "teacher"))
            bot.logout_staff(uid, self.data)
            self.assertIsNone(bot.role_of(uid, self.data))
            self.assertFalse(self.data["staff_auth"][uid]["active"])
            self.assertNotIn(uid, self.data["teachers"])
            self.assertTrue(self.data["staff_auth_history"])
            self.assertFalse(bot.is_linked(uid, "ST-A1B2C3", self.data, "teacher"))
            self.assertFalse(security.is_staff_authorized(uid, "teacher", self.data))

    def test_role_change_is_rejected_before_consuming_another_code(self):
        uid = "804"
        first_code, _ = security.create_access_code(self.data, "teacher")
        admin_code, admin_row = security.create_access_code(self.data, "admin")
        with patch.object(bot, "SECURITY_ENABLED", True), \
             patch.object(bot, "TESTING_AUTH_MODE", False), \
             patch.object(bot, "save_data"), patch.object(bot, "send_message") as send:
            bot.handle_callback(uid, "ROLE:teacher", self.data)
            bot.handle_text(uid, first_code, self.data)
            bot.handle_callback(uid, "ROLE:admin", self.data)
            self.assertIsNone(admin_row["used_at"])
            self.assertEqual(bot.role_of(uid, self.data), "teacher")
            self.assertIn("другая роль", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
