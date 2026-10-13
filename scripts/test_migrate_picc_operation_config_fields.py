# -*- coding: utf-8 -*-

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.quote_platforms.platforms.picc.business import PICC_DELETED_DEFAULT_FIELDS_KEY
from scripts.migrate_picc_operation_config_fields import migrated_values


class PiccOperationConfigMigrationTests(unittest.TestCase):
    def test_removes_account_fields_and_tombstones_but_preserves_products(self):
        before = {
            "第三者责任险": "300",
            "操作机构代码": "36040213",
            "selectedOperateConfigId": "OLD",
            "prpCmain.agentCode": "OLD-AGENT",
            "comCName": "OLD-COMPANY",
            "agentType": "2",
            PICC_DELETED_DEFAULT_FIELDS_KEY: ["业务性质代码", "机动车损失保险"],
        }
        self.assertEqual(
            migrated_values(before),
            {
                "第三者责任险": "300",
                PICC_DELETED_DEFAULT_FIELDS_KEY: ["机动车损失保险"],
            },
        )

    def test_is_idempotent(self):
        value = {"第三者责任险": "300", PICC_DELETED_DEFAULT_FIELDS_KEY: []}
        once = migrated_values(value)
        self.assertEqual(migrated_values(once), once)


if __name__ == "__main__":
    unittest.main()
