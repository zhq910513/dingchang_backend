import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import main


class _FakeRedis:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc

    async def set(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return self.result


class _FakeResult:
    rowcount = 2


class _FakeScalarResult:
    def __init__(self, *, items=None, scalar=None, rowcount=0):
        self._items = items or []
        self._scalar = scalar
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDb:
    def __init__(self):
        self.executed = False
        self.committed = False

    async def execute(self, stmt):
        self.executed = True
        self.statement = stmt
        return _FakeResult()

    async def commit(self):
        self.committed = True


class _FakeDbSequence:
    def __init__(self, results):
        self.results = list(results)
        self.commit_count = 0

    async def execute(self, stmt):
        self.statement = stmt
        if not self.results:
            raise AssertionError("unexpected execute")
        return self.results.pop(0)

    async def commit(self):
        self.commit_count += 1


class OcrPollerRecoveryTest(unittest.TestCase):
    def test_acquire_lock_checked_reports_acquired_busy_and_unavailable(self):
        async def run():
            with patch.object(main, "_redis_client", return_value=_FakeRedis(result=True)):
                with patch.object(main.logger, "info"):
                    token, state = await main.acquire_lock_checked("k", 10)
                self.assertTrue(token)
                self.assertEqual(state, "acquired")

            with patch.object(main, "_redis_client", return_value=_FakeRedis(result=False)):
                token, state = await main.acquire_lock_checked("k", 10)
                self.assertIsNone(token)
                self.assertEqual(state, "busy")

            with (
                patch.object(main, "_redis_client", return_value=_FakeRedis(exc=RuntimeError("down"))),
                patch.object(main.logger, "exception"),
            ):
                token, state = await main.acquire_lock_checked("k", 10)
                self.assertIsNone(token)
                self.assertEqual(state, "unavailable")

        asyncio.run(run())

    def test_reset_stale_processing_tasks_can_be_disabled(self):
        async def run():
            db = _FakeDb()
            with patch.object(main, "OCR_PROCESSING_STALE_SECONDS", 0):
                count = await main._reset_stale_processing_ocr_tasks(db)
            self.assertEqual(count, 0)
            self.assertFalse(db.executed)
            self.assertFalse(db.committed)

        asyncio.run(run())

    def test_reset_stale_processing_tasks_updates_and_commits(self):
        async def run():
            db = _FakeDb()
            with patch.object(main, "OCR_PROCESSING_STALE_SECONDS", 1800):
                with patch.object(main.logger, "warning"):
                    count = await main._reset_stale_processing_ocr_tasks(db)
            self.assertEqual(count, 2)
            self.assertTrue(db.executed)
            self.assertTrue(db.committed)

        asyncio.run(run())

    def test_activate_queued_followup_when_no_active_task_exists(self):
        async def run():
            queued = SimpleNamespace(id=101, scope_id=5588)
            db = _FakeDbSequence(
                [
                    _FakeScalarResult(items=[queued]),
                    _FakeScalarResult(scalar=None),
                    _FakeScalarResult(rowcount=1),
                ]
            )
            with patch.object(main.logger, "info"):
                count = await main._activate_queued_ocr_followups(db)
            self.assertEqual(count, 1)
            self.assertEqual(db.commit_count, 1)

        asyncio.run(run())

    def test_activate_queued_followup_waits_when_active_task_exists(self):
        async def run():
            queued = SimpleNamespace(id=102, scope_id=5588)
            db = _FakeDbSequence(
                [
                    _FakeScalarResult(items=[queued]),
                    _FakeScalarResult(scalar=99),
                ]
            )
            count = await main._activate_queued_ocr_followups(db)
            self.assertEqual(count, 0)
            self.assertEqual(db.commit_count, 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
