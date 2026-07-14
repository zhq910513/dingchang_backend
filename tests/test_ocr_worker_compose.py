import unittest

from app.services.ocr_worker import (
    CORE_OCR_SLOTS,
    _candidate_slot_for_result,
    _compose_extracted_from_slots,
    _filter_blocking_ocr_errors,
    _order_image_signature_from_images,
)


class OcrWorkerComposeTest(unittest.TestCase):
    def test_compose_prefers_driving_license_for_order_fields(self):
        data = {
            "vehicle_cert": {
                "vin": "CERTVIN1234567890",
                "engine_no": "CERTENG",
                "vehicle_model": "CERTMODEL",
                "approved_passenger_count": "5",
                "manufacturer_name": "测试制造厂",
            },
            "driving_license_main": {
                "plate_no": "赣A12345",
                "owner_name": "张三",
                "vin": "DLVIN12345678901",
                "engine_no": "DLENG",
                "vehicle_model": "DLMODEL",
            },
            "idcard_front": {
                "id_name": "李四",
                "id_number": "360100199001011234",
            },
        }

        out = _compose_extracted_from_slots(data)

        self.assertEqual(out["vin"], "DLVIN12345678901")
        self.assertEqual(out["engine_no"], "DLENG")
        self.assertEqual(out["vehicle_model"], "DLMODEL")
        self.assertEqual(out["plate_no"], "赣A12345")
        self.assertEqual(out["owner_name"], "张三")
        self.assertEqual(out["id_number"], "360100199001011234")
        self.assertEqual(out["id_name"], "李四")
        self.assertEqual(out["approved_passenger_count"], "5")
        self.assertEqual(out["manufacturer_name"], "测试制造厂")

    def test_accurate_basic_candidate_maps_by_extracted_fields(self):
        self.assertEqual(
            _candidate_slot_for_result(
                "related",
                "accurate_basic",
                "",
                {"plate_no": "赣A12345", "vin": "LVAV2JVB0JE111269"},
            ),
            "driving_license_main",
        )
        self.assertEqual(
            _candidate_slot_for_result(
                "related",
                "accurate_basic",
                "",
                {"id_number": "360100199001011234", "id_name": "张三"},
            ),
            "idcard_front",
        )
        self.assertEqual(
            _candidate_slot_for_result(
                "related",
                "accurate_basic",
                "",
                {"approved_passenger_count": "5", "manufacturer_name": "测试制造厂"},
            ),
            "vehicle_cert",
        )

    def test_core_ocr_slots_define_related_fallback_boundary(self):
        self.assertEqual(CORE_OCR_SLOTS, {"vehicle_cert", "idcard_front", "driving_license_main"})
        self.assertTrue(CORE_OCR_SLOTS - {"vehicle_cert"})
        self.assertFalse(CORE_OCR_SLOTS - {"vehicle_cert", "idcard_front", "driving_license_main"})

    def test_slot_error_is_non_blocking_when_related_recovers_same_material(self):
        blocking, non_blocking = _filter_blocking_ocr_errors(
            {"vehicle_cert": "template mismatch"},
            slot_extracted={"vehicle_cert": {"vin": "LC0CE4DB1N0113647"}},
            extracted_clean={"vin": "LC0CE4DB1N0113647"},
        )

        self.assertEqual(blocking, {})
        self.assertEqual(non_blocking, {"vehicle_cert": "template mismatch"})

    def test_related_error_is_non_blocking_when_other_image_extracts_order_fields(self):
        blocking, non_blocking = _filter_blocking_ocr_errors(
            {"related#2": "empty result"},
            slot_extracted={"driving_license_main": {"plate_no": "赣BD15038"}},
            extracted_clean={"plate_no": "赣BD15038"},
        )

        self.assertEqual(blocking, {})
        self.assertEqual(non_blocking, {"related#2": "empty result"})

    def test_errors_stay_blocking_when_no_fields_were_extracted(self):
        blocking, non_blocking = _filter_blocking_ocr_errors(
            {"vehicle_cert": "template mismatch"},
            slot_extracted={},
            extracted_clean={},
        )

        self.assertEqual(blocking, {"vehicle_cert": "template mismatch"})
        self.assertEqual(non_blocking, {})

    def test_order_image_signature_is_stable_and_detects_replacement(self):
        class Image:
            def __init__(self, image_id, slot_key, storage_key):
                self.id = image_id
                self.slot_key = slot_key
                self.storage_key = storage_key

        a = [
            Image(2, "related", "backup/b.jpg"),
            Image(1, "vehicle_cert", "cert/a.jpg"),
        ]
        b = [
            Image(1, "vehicle_cert", "cert/a.jpg"),
            Image(2, "related", "backup/b.jpg"),
        ]
        c = [
            Image(1, "vehicle_cert", "cert/a-replaced.jpg"),
            Image(2, "related", "backup/b.jpg"),
        ]

        self.assertEqual(_order_image_signature_from_images(a), _order_image_signature_from_images(b))
        self.assertNotEqual(_order_image_signature_from_images(a), _order_image_signature_from_images(c))


if __name__ == "__main__":
    unittest.main()
