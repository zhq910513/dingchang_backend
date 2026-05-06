# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest import TestCase

from app.schemas.user import UserCreateIn, UserUpdateIn


class UserSchemaRealNameTests(TestCase):
    def test_create_user_accepts_and_trims_real_name(self) -> None:
        payload = UserCreateIn(
            username="sales_demo",
            password="secret123",
            role_name="sales",
            real_name="  张三  ",
            team_name="一队",
        )

        self.assertEqual(payload.real_name, "张三")

    def test_update_user_allows_clearing_real_name(self) -> None:
        payload = UserUpdateIn(real_name="   ")

        self.assertIsNone(payload.real_name)
        if hasattr(payload, "model_dump"):
            data = payload.model_dump(exclude_unset=True)
        else:
            data = payload.dict(exclude_unset=True)
        self.assertIn("real_name", data)
        self.assertIsNone(data["real_name"])
