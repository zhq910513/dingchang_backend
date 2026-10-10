# -*- coding: utf-8 -*-
"""Focused checks for the one-time default config name migration."""

import unittest

from scripts.migrate_joint_sales_config_names import migrated_values
from app.services.quote_platforms.platforms.picc.business import PICC_DELETED_DEFAULT_FIELDS_KEY


class JointSalesConfigMigrationTests(unittest.TestCase):
    def test_migrates_old_name_without_changing_amount_or_other_fields(self):
        before = {
            "途家安顺保费": "398",
            "第三者责任险": "300",
            PICC_DELETED_DEFAULT_FIELDS_KEY: ["车损", "车损"],
        }
        after = migrated_values(before)
        self.assertEqual(after["途顺家安组合保险"], "398")
        self.assertEqual(after["第三者责任险"], "300")
        self.assertEqual(after[PICC_DELETED_DEFAULT_FIELDS_KEY], ["车损", "车损"])
        self.assertEqual(before["途家安顺保费"], "398")

    def test_migrates_deleted_alias_without_resurrecting_field(self):
        before = {"交强": "20", PICC_DELETED_DEFAULT_FIELDS_KEY: ["途家安顺保费"]}
        after = migrated_values(before)
        self.assertNotIn("途顺家安组合保险", after)
        self.assertEqual(after[PICC_DELETED_DEFAULT_FIELDS_KEY], ["途顺家安组合保险"])

    def test_active_value_wins_over_obsolete_deleted_alias(self):
        before = {
            "途顺家安": "500",
            PICC_DELETED_DEFAULT_FIELDS_KEY: ["途家安顺保费"],
        }
        self.assertEqual(migrated_values(before), {
            "途顺家安组合保险": "500",
            PICC_DELETED_DEFAULT_FIELDS_KEY: [],
        })

    def test_does_not_touch_unrelated_config(self):
        before = {"第三者责任险": "300", PICC_DELETED_DEFAULT_FIELDS_KEY: ["车损", "车损"]}
        self.assertEqual(migrated_values(before), before)


if __name__ == "__main__":
    unittest.main()
