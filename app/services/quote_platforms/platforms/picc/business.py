# encoding: utf-8
from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult, QuotePlatformAdapter
from app.services.quote_result_validation import quote_result_has_real_data
from app.services.quote_platforms.platforms.picc.base import (
    KEEPALIVE_PATH,
    KEEPALIVE_PARAMS,
    PiccProtocolClient,
    PiccRequestError,
    PiccSessionExpiredError,
    PiccTransientGatewayError,
    snapshot_from_context,
    success_data,
)
from app.services.quote_platforms.platforms.picc.presentation import (
    picc_is_new_energy_vehicle,
    picc_result_amount_text,
    picc_result_kind_name,
)
from app.services.ocr_cleaner import correct_vehicle_cert_field

NEW_FUEL_ACCOUNT_TYPE = "油车-新"
USED_FUEL_ACCOUNT_TYPE = "油车-旧"
NEW_ENERGY_NEW_ACCOUNT_TYPE = "新能源车-新"
NEW_ENERGY_USED_ACCOUNT_TYPE = "新能源车-旧"
ACCOUNT_TYPE_ALIASES = {
    "新油车": NEW_FUEL_ACCOUNT_TYPE,
    "新车": NEW_FUEL_ACCOUNT_TYPE,
    "燃油新车": NEW_FUEL_ACCOUNT_TYPE,
    "新燃油车": NEW_FUEL_ACCOUNT_TYPE,
    "旧油车": USED_FUEL_ACCOUNT_TYPE,
    "旧车": USED_FUEL_ACCOUNT_TYPE,
    "二手车": USED_FUEL_ACCOUNT_TYPE,
    "过户车": USED_FUEL_ACCOUNT_TYPE,
    "旧燃油车": USED_FUEL_ACCOUNT_TYPE,
    "燃油旧车": USED_FUEL_ACCOUNT_TYPE,
    "新能源新车": NEW_ENERGY_NEW_ACCOUNT_TYPE,
    "新新能源车": NEW_ENERGY_NEW_ACCOUNT_TYPE,
    "新能源车新": NEW_ENERGY_NEW_ACCOUNT_TYPE,
    "旧能源车": NEW_ENERGY_USED_ACCOUNT_TYPE,
    "新能源旧车": NEW_ENERGY_USED_ACCOUNT_TYPE,
    "旧新能源车": NEW_ENERGY_USED_ACCOUNT_TYPE,
    "新能源车旧": NEW_ENERGY_USED_ACCOUNT_TYPE,
}

VEHICLE_QUERY_PATH = "/khyx/newFront/qth/vehicle/jyQuery.do"
PRECISE_VEHICLE_QUERY_PATH = "/khyx/newFront/qth/vehicle/QtPrpPreciseVehicleQuery.do"
TAXABATE_QUERY_PATH = "/khyx/newFront/qtr/price/queryQtTaxabate.do"
CAL_ACTUAL_VALUE_PATH = "/khyx/newFront/price/calActualVal.do"
VERIFY_AGENT_CONTROL_PATH = "/khyx/newFront/qth/price/verifyPersonalAgtControl.do"
DUPLICATE_INSURED_VIN_PATH = "/khyx/newFront/qth/price/duplicateInsuredVinNo.do"
QUERY_INSURED_BY_CAR_INFO_PATH = "/khyx/newFront/qth/price/queryInsuredByCarInfo.do"
JOINT_SALE_PLAN_INFO_PATH = "/khyx/newFront/prpall/common/choosePlanInfoForJointSale.do"
MONOPOLY_QUERY_PATH = "/khyx/newFront/qth/myinfo/monopoly/query.do"
QUERY_QUALITY_FLAG_PATH = "/khyx/newFront/qth/price/queryQualityFlag.do"
GET_CLUB_GIFT_DISPLAY_INFO_PATH = "/khyx/newFront/qth/price/getClubGiftDisplayInfo.do"
QUERY_CAR_CHECKER_PATH = "/khyx/newFront/common/queryCarchecker.do"
GET_CURRENT_TIME_PATH = "/khyx/newFront/price/getCurrentTime.do"
RENEWAL_CHECK_OWNER_PATH = "/khyx/newFront/price/checkIsOwner.do"
RENEWAL_QUOTE_SEARCH_PATH = "/khyx/newFront/qth/price/quoteRenew.do"
RENEWAL_QUOTE_POLICY_PATH = "/khyx/newFront/qth/price/quotePolicy.do"
QUOTE_PATH = "/khyx/newFront/qth/price/quote.do"
QUERY_QUOTE_TIMES_PATH = "/khyx/newFront/qth/price/queryQuoteTimes.do"
CLEAR_JS_QUOTATION_NO_PATH = "/khyx/newFront/qth/price/clearJSQuotationNo.do"
RENEWAL_POLICY_AES_KEY = b"8F6B2AK33DZE20A05E74C231B47AC8F9"
TZ_BJ = timezone(timedelta(hours=8))

JOINT_SALES_QUOTATION_FIELDS = {
    "EAD": "quotationNoEAD",
    "EBS": "quotationNoEBS",
    "JAH": "quotationNoJAH",
    "LAI": "quotationNoLAI",
    "YEL": "quotationNoYEL",
    "ZDB": "quotationNoZDB",
}

PRODUCT_COMPULSORY = "交强"
PRODUCT_LOSS = "机动车损失保险"
PRODUCT_THIRD_PARTY = "第三者责任险"
PRODUCT_DRIVER = "车上人员责任险（司机）"
PRODUCT_PASSENGER = "车上人员责任险（乘客）"
PRODUCT_SHARED_LIMIT = "共享主险限额"
PRODUCT_MEDICAL_THIRD = "医保外医疗费用责任险（第三者责任险）"
PRODUCT_ROAD_RESCUE = "机动车增值服务特约条款（道路救援服务）"
PRODUCT_EXTERNAL_GRID = "附加外部电网故障损失险"
PRODUCT_TUJIA_ANSHUN_PREMIUM = "途家安顺保费"
PRODUCT_EXCLUSIONS_KEY = "quote_product_exclusions"

PRODUCT_FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    PRODUCT_COMPULSORY: ("交强", "交强险"),
    PRODUCT_LOSS: ("机动车损失保险", "车损险", "车辆损失险", "车损"),
    PRODUCT_THIRD_PARTY: ("第三者责任险", "第三责任险", "三者险", "三者", "三责"),
    PRODUCT_DRIVER: ("车上人员责任险（司机）", "车上人员责任险(司机)", "司机险", "司机责任险", "司机"),
    PRODUCT_PASSENGER: ("车上人员责任险（乘客）", "车上人员责任险(乘客)", "乘客险", "乘客责任险", "乘客"),
    PRODUCT_SHARED_LIMIT: ("共享主险限额", "主险限额共享"),
    PRODUCT_MEDICAL_THIRD: ("医保外医疗费用责任险（第三者责任险）", "医保外医疗费用责任险(第三者责任险)", "医保外三者", "医保外"),
    PRODUCT_ROAD_RESCUE: (
        "机动车增值服务特约条款（道路救援服务）",
        "附加机动车增值服务特约条款（道路救援服务）",
        "道路救援服务",
        "道路救援",
        "救援",
    ),
    PRODUCT_EXTERNAL_GRID: (
        "附加外部电网故障损失险",
        "外部电网故障损失险",
        "外部电网",
        "电网故障损失险",
        "电网故障",
    ),
    PRODUCT_TUJIA_ANSHUN_PREMIUM: ("途家安顺保费", "途家安顺", "途家安顺非车保费"),
}

PICC_TUJIA_ANSHUN_RISK_CODE = "LCO"
PICC_TUJIA_ANSHUN_BRAND_ID = "BTA1"
PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE = "05"

PICC_KIND_NAME_BY_CODE = {
    "051050": "机动车损失保险",
    "051051": "机动车第三者责任保险",
    "051052": "机动车车上人员责任保险（司机）",
    "051053": "机动车车上人员责任保险（乘客）",
    "051063": "附加医保外医疗费用责任险（机动车第三者责任保险）",
    "051064": "附加机动车增值服务特约条款（道路救援服务）",
    "051074": "机动车交通事故责任强制保险",
    "051085": "附加外部电网故障损失险",
}

PICC_CORE_MOTOR_KIND_CODES = frozenset({
    "051050",
    "051051",
    "051052",
    "051053",
})

PICC_REAL_QUOTE_ACCOUNT_TYPES = {
    NEW_FUEL_ACCOUNT_TYPE,
    USED_FUEL_ACCOUNT_TYPE,
    NEW_ENERGY_NEW_ACCOUNT_TYPE,
    NEW_ENERGY_USED_ACCOUNT_TYPE,
}


def _picc_quote_result_has_real_premium(result: Any) -> bool:
    """Keep PICC on the shared, strict quote-result truthfulness rule."""
    return quote_result_has_real_data(result)
PICC_MOTOR_QUOTE_PROFILES: Dict[str, Dict[str, Any]] = {
    NEW_FUEL_ACCOUNT_TYPE: {
        "account_type_name": NEW_FUEL_ACCOUNT_TYPE,
        "request_id_prefix": "picc-new-fuel",
        "mode": "picc_new_fuel_real",
        "display_name": "人保油车-新报价",
        "energy_type_plat": "0",
        "energy_type_name": "燃油",
        "vehicle_energy_type": "0",
        "is_energy_car": "0",
        "energy_flag": "0",
        "vehicle_fuel_type": "D1",
        "license_type": "02",
        "license_color_code": "01",
        "tax_type": "1",
        "tax_calculate_mode": "C1",
        "tax_abate_type": "1",
        "fuel_type": "A",
        "new_car_flag": "on",
        "include_pay_last_year": False,
        "license_no_strategy": "new_car_placeholder",
        "enroll_date_fallback": "today",
        "vehicle_query_resources": "0524",
        "product_defaults": {
            PRODUCT_COMPULSORY: "20",
            PRODUCT_THIRD_PARTY: "300",
            PRODUCT_DRIVER: "2",
            PRODUCT_PASSENGER: "2",
            PRODUCT_MEDICAL_THIRD: "300",
            PRODUCT_SHARED_LIMIT: True,
        },
    },
    USED_FUEL_ACCOUNT_TYPE: {
        "account_type_name": USED_FUEL_ACCOUNT_TYPE,
        "request_id_prefix": "picc-used-fuel",
        "mode": "picc_used_fuel_real",
        "display_name": "人保油车-旧报价",
        "energy_type_plat": "0",
        "energy_type_name": "燃油",
        "vehicle_energy_type": "0",
        "is_energy_car": "0",
        "energy_flag": "0",
        "vehicle_fuel_type": "D1",
        "license_type": "02",
        "license_color_code": "01",
        "tax_type": "1",
        "tax_calculate_mode": "C1",
        "tax_abate_type": "1",
        "fuel_type": "A",
        "new_car_flag": "",
        "include_pay_last_year": True,
        "license_no_strategy": "required",
        "enroll_date_fallback": "",
        "vehicle_query_resources": "0524",
        "product_defaults": {
            PRODUCT_COMPULSORY: "20",
            PRODUCT_THIRD_PARTY: "300",
            PRODUCT_DRIVER: "2",
            PRODUCT_PASSENGER: "2",
            PRODUCT_MEDICAL_THIRD: "300",
            PRODUCT_SHARED_LIMIT: True,
        },
    },
    NEW_ENERGY_NEW_ACCOUNT_TYPE: {
        "account_type_name": NEW_ENERGY_NEW_ACCOUNT_TYPE,
        "request_id_prefix": "picc-new-energy-new",
        "mode": "picc_new_energy_new_real",
        "display_name": "人保新能源车-新报价",
        "energy_type_plat": "1",
        "energy_type_name": "纯电动",
        "vehicle_energy_type": "1",
        "is_energy_car": "1",
        "energy_flag": "1",
        "vehicle_fuel_type": "D6",
        "license_type": "52",
        "license_color_code": "52",
        "tax_type": "2",
        "tax_calculate_mode": "C1",
        "tax_abate_type": "1",
        "fuel_type": "A",
        "new_car_flag": "on",
        "include_pay_last_year": False,
        "license_no_strategy": "new_car_placeholder",
        "enroll_date_fallback": "today",
        # Keep 0524 until a verified NE-only resource code is confirmed by HAR.
        "vehicle_query_resources": "0524",
        "product_defaults": {
            PRODUCT_COMPULSORY: "20",
            PRODUCT_THIRD_PARTY: "300",
            PRODUCT_DRIVER: "5",
            PRODUCT_PASSENGER: "5",
            PRODUCT_MEDICAL_THIRD: "300",
            PRODUCT_SHARED_LIMIT: True,
        },
    },
    NEW_ENERGY_USED_ACCOUNT_TYPE: {
        "account_type_name": NEW_ENERGY_USED_ACCOUNT_TYPE,
        "request_id_prefix": "picc-new-energy-used",
        "mode": "picc_new_energy_used_real",
        "display_name": "人保新能源车-旧报价",
        "energy_type_plat": "1",
        "energy_type_name": "纯电动",
        "vehicle_energy_type": "1",
        "is_energy_car": "1",
        "energy_flag": "1",
        "vehicle_fuel_type": "D6",
        "license_type": "52",
        "license_color_code": "52",
        "tax_type": "2",
        "tax_calculate_mode": "C1",
        "tax_abate_type": "1",
        "fuel_type": "A",
        "new_car_flag": "",
        "include_pay_last_year": True,
        "license_no_strategy": "required",
        "enroll_date_fallback": "",
        "vehicle_query_resources": "0524",
        "product_defaults": {
            PRODUCT_COMPULSORY: "20",
            PRODUCT_THIRD_PARTY: "300",
            PRODUCT_DRIVER: "4",
            PRODUCT_PASSENGER: "4",
            PRODUCT_MEDICAL_THIRD: "300",
            PRODUCT_SHARED_LIMIT: True,
        },
    },
}

PICC_COMMON_PLATFORM_DEFAULTS: Dict[str, Any] = {
    "归属机构代码": "36040200",
    "操作机构代码": "36040213",
    "操作配置ID": "QT360402131762390540324",
    "验车人工号": "24090664",
    "验车人姓名": "陈宛杰",
    "送修码启用": "1",
    "送修码": "3604731000027",
    "送修码名称": "濂溪区金鑫汽车修理厂",
    "专管代码": "3604731000027",
    "专管名称": "濂溪区金鑫汽车修理厂",
    "monopolyCode": "3604731000027",
    "monopolyName": "濂溪区金鑫汽车修理厂",
    "查询区域代码": "360000",
    "税务机关代码": "13604010000",
    "税务机关名称": "国家税务总局九江市税务局第一税务分局",
    "业务性质代码": "2",
    "业务性质名称": "专业代理业务",
    "是否代收车船税": "1",
    "行驶区域代码": "11",
    "条款类型": "F42",
    "国产进口标识": "01",
    "纳税人类型": "01",
    "车辆颜色代码": "999",
    "车主与被保险人关系": "所有",
    "车主类型": "1",
    "车主性别": "1",
    "车主生日": "1990-01-01",
}

PICC_ENERGY_TYPE_PLAT_LABELS: Dict[str, str] = {
    "0": "燃油",
    "1": "纯电动",
    "2": "燃料电池",
    "3": "插电式混合动力",
    "4": "增程式混合动力",
}
PICC_VEHICLE_FUEL_TYPE_TO_ENERGY_TYPE_PLAT: Dict[str, str] = {
    "D6": "1",
    "D12": "3",
}
PICC_PM_FUEL_TYPE_TO_ENERGY_TYPE_PLAT: Dict[str, str] = {
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}


def picc_motor_builtin_default_values(account_type_name: Any) -> Dict[str, Any]:
    """Return editable PICC product defaults for the configured account type."""
    profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
    product_defaults = _json_obj(profile.get("product_defaults"))
    return {
        PRODUCT_TUJIA_ANSHUN_PREMIUM: "398",
        PRODUCT_COMPULSORY: product_defaults.get(PRODUCT_COMPULSORY, "20"),
        PRODUCT_LOSS: product_defaults.get(PRODUCT_LOSS, ""),
        PRODUCT_THIRD_PARTY: product_defaults.get(PRODUCT_THIRD_PARTY, "300"),
        PRODUCT_DRIVER: product_defaults.get(PRODUCT_DRIVER, "2"),
        PRODUCT_PASSENGER: product_defaults.get(PRODUCT_PASSENGER, "2"),
        PRODUCT_SHARED_LIMIT: product_defaults.get(PRODUCT_SHARED_LIMIT, True),
        PRODUCT_MEDICAL_THIRD: product_defaults.get(PRODUCT_MEDICAL_THIRD, "300"),
        PRODUCT_EXTERNAL_GRID: "",
    }


def _picc_business_defaults(default_values: Any) -> Dict[str, Any]:
    """Merge hidden protocol defaults behind the editable PICC product config."""
    defaults = dict(PICC_COMMON_PLATFORM_DEFAULTS)
    defaults.update(_json_obj(default_values))
    return defaults


PICC_VEHICLE_CERT_FIELD_MAP: Dict[str, str] = {
    "plateNo": "plate_no",
    "plate_no": "plate_no",
    "plateNumber": "plate_no",
    "plate_number": "plate_no",
    "licenseNo": "plate_no",
    "license_no": "plate_no",
    "licensePlateNo": "plate_no",
    "license_plate_no": "plate_no",
    "licensePlateNumber": "plate_no",
    "license_plate_number": "plate_no",
    "prpCitemCar.licenseNo": "plate_no",
    "vin": "vin",
    "vinNo": "vin",
    "vin_no": "vin",
    "vinno": "vin",
    "vehicleVin": "vin",
    "vehicle_vin": "vin",
    "carVin": "vin",
    "car_vin": "vin",
    "frameNo": "vin",
    "frame_no": "vin",
    "chassisNo": "vin",
    "chassis_no": "vin",
    "jyVehicleRequest.vinno": "vin",
    "prpCitemCar.vinNo": "vin",
    "prpCitemCar.frameNo": "vin",
    "engine": "engine_no",
    "engineNo": "engine_no",
    "engine_no": "engine_no",
    "engineNumber": "engine_no",
    "engine_number": "engine_no",
    "motorNo": "engine_no",
    "motor_no": "engine_no",
    "prpCitemCar.engineNo": "engine_no",
    "idNo": "id_number",
    "id_no": "id_number",
    "id_number": "id_number",
    "ownerIdNo": "id_number",
    "owner_id_no": "id_number",
    "ownerCertNo": "id_number",
    "owner_cert_no": "id_number",
    "insuredIdNo": "id_number",
    "insured_id_no": "id_number",
    "applicantIdNo": "id_number",
    "applicant_id_no": "id_number",
    "identifyNumber": "id_number",
    "identify_number": "id_number",
    "quoteCarOwner.identifyNumber": "id_number",
    "carQuoteInsuredRealList[0].holdIdentifyNumber": "id_number",
    "carQuoteInsuredRealList[1].holdIdentifyNumber": "id_number",
    "carQuoteInsuredRealList[2].holdIdentifyNumber": "id_number",
    "certNo": "cert_no",
    "certificateNo": "certificate_no",
    "certificate_no": "certificate_no",
    "cert_no": "cert_no",
    "vehicle_certificate_no": "vehicle_certificate_no",
    "chassis_cert_no": "chassis_cert_no",
}
PICC_VEHICLE_CERT_FIELD_MAP_LOWER: Dict[str, str] = {
    str(key).lower(): value for key, value in PICC_VEHICLE_CERT_FIELD_MAP.items()
}


def _clean_vehicle_cert_value(field_name: str, value: Any) -> Any:
    text = _to_str(value).strip()
    if not text:
        return value
    cleaned = correct_vehicle_cert_field(field_name, text)
    return cleaned or value


def _clean_vehicle_cert_fields(data: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(_json_obj(data))
    for key in list(out.keys()):
        key_text = _to_str(key).strip()
        key_lower = key_text.lower()
        field_name = PICC_VEHICLE_CERT_FIELD_MAP.get(key_text) or PICC_VEHICLE_CERT_FIELD_MAP_LOWER.get(key_lower)
        if not field_name:
            if key_lower.endswith(".holdidentifynumber") or key_lower.endswith("holdidentifynumber"):
                field_name = "id_number"
            elif key_lower.endswith(".identifynumber") or key_lower.endswith("identifynumber"):
                field_name = "id_number"
            elif key_lower.endswith(".owneridno") or key_lower.endswith("owneridno") or key_lower.endswith("owner_id_no"):
                field_name = "id_number"
            elif key_lower.endswith(".ownercertno") or key_lower.endswith("ownercertno") or key_lower.endswith("owner_cert_no"):
                field_name = "id_number"
            elif key_lower.endswith(".insuredidno") or key_lower.endswith("insuredidno") or key_lower.endswith("insured_id_no"):
                field_name = "id_number"
            elif key_lower.endswith(".applicantidno") or key_lower.endswith("applicantidno") or key_lower.endswith("applicant_id_no"):
                field_name = "id_number"
            elif key_lower.endswith(".idno") or key_lower.endswith("idno") or key_lower.endswith("id_number"):
                field_name = "id_number"
            elif key_lower.endswith(".vinno") or key_lower.endswith("vinno") or key_lower.endswith("vin_no"):
                field_name = "vin"
            elif key_lower.endswith(".frameno") or key_lower.endswith("frameno") or key_lower.endswith("frame_no"):
                field_name = "vin"
            elif key_lower.endswith(".chassisno") or key_lower.endswith("chassisno") or key_lower.endswith("chassis_no"):
                field_name = "vin"
            elif key_lower.endswith(".vehiclevin") or key_lower.endswith("vehiclevin") or key_lower.endswith("vehicle_vin"):
                field_name = "vin"
            elif key_lower.endswith(".engineno") or key_lower.endswith("engineno") or key_lower.endswith("engine_no"):
                field_name = "engine_no"
            elif key_lower.endswith(".enginenumber") or key_lower.endswith("enginenumber") or key_lower.endswith("engine_number"):
                field_name = "engine_no"
            elif key_lower.endswith(".licenseno") or key_lower.endswith("licenseno") or key_lower.endswith("license_plate_no"):
                field_name = "plate_no"
            elif key_lower.endswith(".licenseplatenumber") or key_lower.endswith("licenseplatenumber") or key_lower.endswith("license_plate_number"):
                field_name = "plate_no"
        if field_name:
            out[key] = _clean_vehicle_cert_value(field_name, out.get(key))
    return out


def _clean_used_fuel_request_body(request_body: Mapping[str, Any]) -> Dict[str, Any]:
    """Clean vehicle certificate numbers in every known PICC request-body section."""
    body = dict(_json_obj(request_body))
    for section in ("vehicleForm", "ownerForm", "quoteForm", "vehicle", "applicant"):
        section_data = body.get(section)
        if isinstance(section_data, Mapping):
            body[section] = _clean_vehicle_cert_fields(section_data)
    return body

USED_FUEL_QUOTE_EMPTY_FORM_FIELDS: tuple[str, ...] = (
    "activityID",
    "allMobile",
    "allMobile2",
    "allMobileECIF",
    "allMobileInsured",
    "businessPropertyCode",
    "carQuoteInsuredRealList[0].holdIdentifyNumber",
    "carQuoteInsuredRealList[0].holdName",
    "carQuoteInsuredRealList[1].holdIdentifyNumber",
    "carQuoteInsuredRealList[1].holdName",
    "carQuoteInsuredRealList[2].holdIdentifyNumber",
    "carQuoteInsuredRealList[2].holdName",
    "checkAnswer",
    "checkAnswerCI",
    "clubGiftPackageDesStr",
    "contactId",
    "contactId2",
    "contactIdECIF",
    "contactIdInsured",
    "custId",
    "custRelContactID",
    "customerID",
    "detailIdLCO",
    "deviceList[0].actualvalue",
    "deviceList[0].buydate",
    "deviceList[0].devicename",
    "deviceList[0].purchaseprice",
    "deviceList[0].quantity",
    "eadinfo.eachCopies",
    "eadinfo.flag",
    "eadinfo.insuredCount",
    "eadinfo.totalCopies",
    "ecifUserTypeCode",
    "feProjectCode",
    "fixedSchemeRation.comCode",
    "fixedSchemeRation.rationName",
    "fixedSchemeRation.rationType",
    "giftPackageComCode",
    "giftPackageId",
    "hardWareEquipments[0].amount",
    "hardWareEquipments[0].deviceModel",
    "hardWareEquipments[0].deviceName",
    "importantProjectCode",
    "lastIdentifyNo",
    "lastPolicyNo",
    "marketFeeRateChgBI",
    "monopolyCode",
    "monopolyName",
    "netTPPrpCyelInfo.yelFlag",
    "newCarFlag",
    "ocrIds",
    "oldQuotationId",
    "piid",
    "preDiscount",
    "projectCodeDes",
    "prpCcarShipTax.dutyPaidProofNo",
    "prpCcarShipTax.payEndDate",
    "prpCcarShipTax.payStartDate",
    "prpCcarShipTax.taxAbateAmount",
    "prpCcarShipTax.taxAbateProportion",
    "prpCcarShipTax.taxAbateReason",
    "prpCcarShipTax.taxDocumentDate",
    "prpCcarShipTax.taxPaidAreaCode",
    "prpCcarShipTax.taxPayerCode",
    "prpCcarShipTax.taxPayerName",
    "prpCcarShipTax.taxPayerNumber",
    "prpCitemCar.certificateDate",
    "prpCitemCar.cylindercount",
    "prpCitemCar.familyId",
    "prpCitemCar.issueDate",
    "prpCitemCar.lastBIPolicyNo",
    "prpCitemCar.lastCIPolicyNo",
    "prpCitemCar.lastCarChecker",
    "prpCitemCar.lastEndDateBI",
    "prpCitemCar.lastEndDateCI",
    "prpCitemCar.lastUserclassificationCode",
    "prpCitemCar.licenseNo1",
    "prpCitemCar.licenseNo2",
    "prpCitemCar.loanName",
    "prpCitemCar.localLicense",
    "prpCitemCar.localUse",
    "prpCitemCar.modelCodeAlias",
    "prpCitemCar.runMiles",
    "prpCitemCarExt.lastDamaged",
    "prpCitemCarExt.lastDamagedA",
    "prpCitemCarExt.lastDamagedBI",
    "prpCitemCarExt.lastDamagedCI",
    "prpCitemCarExt.noDamYearsBI",
    "prpCitemCarExt.thisDamagedBI",
    "prpCitemKindsTemp[1].amount",
    "prpCitemKindsTemp[1].benchMarkPremium",
    "prpCitemKindsTemp[1].calculateFlag",
    "prpCitemKindsTemp[1].chooseFlag",
    "prpCitemKindsTemp[1].disCount",
    "prpCitemKindsTemp[1].endDate",
    "prpCitemKindsTemp[1].endHour",
    "prpCitemKindsTemp[1].flag",
    "prpCitemKindsTemp[1].premium",
    "prpCitemKindsTemp[1].rate",
    "prpCitemKindsTemp[1].startDate",
    "prpCitemKindsTemp[1].startHour",
    "prpCmain.adjustSelfPricingDiscount",
    "prpCmain.agentCode",
    "prpCmain.businesNature",
    "prpCmain.cashComMarketRateDownRatio",
    "prpCmain.claimRiskLevel",
    "prpCmain.dwhzTaskId",
    "prpCmain.endDate",
    "prpCmain.handler1Code",
    "prpCmain.handlerCode",
    "prpCmain.lastAgentCode",
    "prpCmain.locationCityMatched",
    "prpCmain.operateCode",
    "prpCmain.opportunityId",
    "prpCmain.presaleCarFlag",
    "prpCmain.projectCode",
    "prpCmain.proposalNo",
    "prpCmain.vehicleStyleUniqueId",
    "quickMobileInsured",
    "quickMobileRel",
    "quotationNoEAD",
    "quotationNoEBS",
    "quotationNoJAH",
    "quotationNoLAI",
    "quotationNoYEL",
    "quotationNoZDB",
    "quoteCarOwner.age",
    "quoteInsured.age",
    "quoteInsured.birthday",
    "quoteInsured.insuredType",
    "quoteInsured.sex",
    "quoteMsgSource",
    "relCustID",
    "replaceableNum",
    "softWareEquipments[0].amount",
    "softWareEquipments[0].deviceEndDate",
    "softWareEquipments[0].deviceModel",
    "softWareEquipments[0].deviceName",
    "softWareEquipments[0].deviceStartDate",
    "softWareEquipments[0].longValue",
    "softWareEquipments[0].remark",
    "taskId",
    "transferDate",
    "vehicleStyle",
    "zhuanbaobianswer.answer",
    "zhuanbaobianswer.queryseuqenceno",
    "zhuanbaocianswer.answer",
    "zhuanbaocianswer.queryseuqenceno",
)


class PiccDuplicateQuoteError(PiccRequestError):
    pass


class PiccQuotaFullError(PiccRequestError):
    pass


class PiccBusinessRequestError(PiccRequestError):
    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        platform_response: Any = None,
        request_body: Optional[Mapping[str, Any]] = None,
        platform_auto_notices: Optional[List[Mapping[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.action = action
        self.platform_response = platform_response
        self.request_body = dict(request_body or {})
        self.platform_auto_notices = [dict(item or {}) for item in (platform_auto_notices or []) if isinstance(item, Mapping)]


def _json_obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_obj_loose(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _deep_merge(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(defaults or {})
    for key, value in dict(overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _to_str(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize_account_type(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    return ACCOUNT_TYPE_ALIASES.get(text, text)


def _motor_quote_profile(account_type_name: Any) -> Dict[str, Any]:
    normalized = _normalize_account_type(account_type_name)
    profile = PICC_MOTOR_QUOTE_PROFILES.get(normalized)
    return dict(profile) if isinstance(profile, Mapping) else {}


def _profile_text(profile: Mapping[str, Any], key: str, fallback: Any = "") -> str:
    return _to_str(profile.get(key, fallback)).strip()


def _profile_bool(profile: Mapping[str, Any], key: str, default: bool = False) -> bool:
    if key not in profile:
        return default
    return _checked(profile.get(key), default=default)


def _profile_product_default(
    defaults: Mapping[str, Any],
    profile: Mapping[str, Any],
    canonical_name: str,
    fallback: Any = "",
) -> Any:
    profile_defaults = _json_obj(profile.get("product_defaults"))
    return _default_value(defaults, canonical_name, profile_defaults.get(canonical_name, fallback))


def _canonical_product_name(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    if not text:
        return ""
    low = text.lower()
    for canonical, aliases in PRODUCT_FIELD_ALIASES.items():
        candidates = {canonical, *aliases}
        for alias in candidates:
            alias_text = re.sub(r"\s+", "", _to_str(alias).strip())
            if alias_text and (text == alias_text or low == alias_text.lower()):
                return canonical
    return text


def _product_exclusions(defaults: Mapping[str, Any]) -> set[str]:
    raw = None
    for key in (PRODUCT_EXCLUSIONS_KEY, "禁用险种", "排除险种"):
        if key in defaults:
            raw = defaults.get(key)
            break
    if raw is None:
        return set()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return set()
        parsed: Any = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
        items = parsed if isinstance(parsed, list) else re.split(r"[,，、;；\s]+", text)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    return {name for item in items if (name := _canonical_product_name(item))}


def _product_excluded(defaults: Mapping[str, Any], product_name: str) -> bool:
    return _canonical_product_name(product_name) in _product_exclusions(defaults)


def _first_text(*values: Any) -> str:
    for value in values:
        text = _to_str(value).strip()
        if text:
            return text
    return ""


def _has_text(value: Any) -> bool:
    return _to_str(value).strip() != ""


def _money(value: Any, default: str = "0") -> Decimal:
    text = _to_str(value).strip().replace(",", "")
    if text and not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        text = match.group(0) if match else ""
    if not text:
        text = default
    try:
        return Decimal(text)
    except Exception:
        return Decimal(default)


def _money_text(value: Any) -> str:
    return str(_money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _money_text_or_empty(value: Any) -> str:
    return _money_text(value) if _has_text(value) else ""


def _is_positive_amount(value: Any) -> bool:
    if not _has_text(value):
        return False
    return _money(value) > Decimal("0")


def _clean_money_text(value: Any, default: str = "0") -> str:
    amount = _money(value, default)
    if amount == amount.to_integral():
        return str(int(amount))
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _clean_money_text_or_empty(value: Any) -> str:
    return _clean_money_text(value) if _has_text(value) else ""


def _picc_encrypt_renewal_policy_no(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    raw = text.encode("utf-8")
    pad_size = 16 - (len(raw) % 16)
    padded = raw + bytes([pad_size]) * pad_size
    encryptor = Cipher(algorithms.AES(RENEWAL_POLICY_AES_KEY), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _int_text(value: Any, default: str = "0") -> str:
    try:
        return str(int(_money(value, default)))
    except Exception:
        return str(default)


def _safe_int_local(value: Any, default: int = 0) -> int:
    try:
        return int(_money(value, str(default)))
    except Exception:
        return default


def _date_text(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if not m:
        return ""
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _platform_datetime_parts(value: Any) -> Dict[str, str]:
    """Parse PICC date values while preserving optional hour/minute."""
    text = _to_str(value).strip()
    day = _date_text(text)
    if not day:
        return {}
    out = {"date": day, "hour": "", "minute": ""}
    match = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", text)
    if not match:
        return out
    suffix = re.sub(r"^[\sT]+", "", text[match.end():])
    time_match = re.match(r"^(\d{1,2})(?:\s*[:：时点]\s*(\d{1,2}))?(?:分)?", suffix)
    if not time_match:
        return out
    hour = _safe_int_local(time_match.group(1), -1)
    minute = _safe_int_local(time_match.group(2), 0) if time_match.group(2) is not None else 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        out["hour"] = str(hour)
        out["minute"] = str(minute)
    return out


def _next_day_text() -> str:
    return (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


def _today_text() -> str:
    return date.today().strftime("%Y-%m-%d")


def _parse_date(value: Any) -> Optional[date]:
    text = _date_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _end_date_text(start_date: Any) -> str:
    start = _parse_date(start_date)
    if not start:
        return ""
    try:
        next_year = start.replace(year=start.year + 1)
    except ValueError:
        next_year = start.replace(year=start.year + 1, day=28)
    return (next_year - timedelta(days=1)).strftime("%Y-%m-%d")


def _period_time_texts(hour: Any = "", minute: Any = "") -> tuple[str, str]:
    parsed_hour = _safe_int_local(hour, -1)
    parsed_minute = _safe_int_local(minute, 0) if _has_text(minute) else 0
    if 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59:
        return str(parsed_hour), str(parsed_minute)
    return "0", "0"


def _period_time_explicit(hour: Any = "", minute: Any = "") -> bool:
    if not _has_text(hour):
        return False
    parsed_hour = _safe_int_local(hour, -1)
    parsed_minute = _safe_int_local(minute, 0) if _has_text(minute) else 0
    return 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59


def _ci_end_date_text(start_date: Any, start_hour: Any = "", start_minute: Any = "") -> str:
    start = _parse_date(start_date)
    if not start:
        return ""
    hour, minute = _period_time_texts(start_hour, start_minute)
    if hour == "0" and minute == "0":
        return _end_date_text(start_date)
    try:
        return start.replace(year=start.year + 1).strftime("%Y-%m-%d")
    except ValueError:
        return start.replace(year=start.year + 1, day=28).strftime("%Y-%m-%d")


def _renewal_next_start_date(end_date: Any) -> str:
    end_day = _parse_date(end_date)
    if not end_day:
        return _next_day_text()
    return (end_day + timedelta(days=1)).strftime("%Y-%m-%d")


def _year_start_date(value: Any) -> str:
    day = _parse_date(value)
    return f"{day.year}-01-01" if day else ""


def _year_end_date(value: Any) -> str:
    day = _parse_date(value)
    return f"{day.year}-12-31" if day else ""


def _use_years(enroll_date: Any, today: Optional[date] = None) -> str:
    start = _parse_date(enroll_date)
    if not start:
        return ""
    current = today or date.today()
    return str(max(0, current.year - start.year))


def _period_last_year(start_date: Any) -> str:
    start = _parse_date(start_date)
    if not start:
        return str(date.today().year - 1)
    return str(start.year - 1)


def _default_value(defaults: Mapping[str, Any], canonical_name: str, fallback: Any = "") -> Any:
    for key in PRODUCT_FIELD_ALIASES.get(canonical_name, (canonical_name,)):
        if key in defaults and _to_str(defaults.get(key)).strip() != "":
            return defaults.get(key)
    if canonical_name in defaults and _to_str(defaults.get(canonical_name)).strip() != "":
        return defaults.get(canonical_name)
    return fallback


def _has_configured_product_default(defaults: Mapping[str, Any], canonical_name: str) -> bool:
    for key in PRODUCT_FIELD_ALIASES.get(canonical_name, (canonical_name,)):
        if key in defaults and _to_str(defaults.get(key)).strip() != "":
            return True
    return canonical_name in defaults and _to_str(defaults.get(canonical_name)).strip() != ""


def _field_value(defaults: Mapping[str, Any], *names: str, fallback: Any = "") -> Any:
    for name in names:
        if name in defaults and _to_str(defaults.get(name)).strip() != "":
            return defaults.get(name)
    return fallback


def _wan_or_amount_to_amount(value: Any, fallback_wan: str) -> str:
    amount = _money(value, fallback_wan)
    if amount < Decimal("10000"):
        amount *= Decimal("10000")
    return str(int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _wan_or_amount_to_wan_text(value: Any, fallback_wan: str) -> str:
    amount = _money(value, fallback_wan)
    if amount >= Decimal("10000"):
        amount = (amount / Decimal("10000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return _clean_money_text(amount, fallback_wan)


def _external_grid_amount(defaults: Mapping[str, Any], actual_value: Any) -> str:
    raw = _default_value(defaults, PRODUCT_EXTERNAL_GRID, "")
    text = _to_str(raw).strip()
    if not text:
        return ""
    low = text.lower()
    if low in {"0", "false", "no", "n", "否", "不", "不选", "不勾选", "取消", "关闭", "去掉"}:
        return ""
    if low in {"1", "true", "yes", "y", "是", "选", "勾选", "开启", "打开", "跟随车损", "实际价值", "车损"}:
        return _clean_money_text(actual_value)
    amount = _money(raw, "0")
    if amount <= 0:
        return ""
    if amount < Decimal("10000"):
        amount *= Decimal("10000")
    return _clean_money_text(amount)


def _strip_platform_error_code(message: Any) -> str:
    text = html.unescape(_to_str(message)).strip()
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?[^>]+>", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^[A-Za-z0-9_]+\s*\n?[:：]", "", text).strip()
    text = re.sub(r"[!,，|]*[A-Z]{2,}[A-Z0-9]+(?:\|[A-Z]{2,}[A-Z0-9]+)?$", "", text).strip()
    text = re.sub(r"^[A-Z]{2,}[A-Z0-9]*\d*(?:-\d+)?", "", text).lstrip("-_：:，, ")
    return text.strip()


def _platform_status_code(data: Any) -> int:
    payload = _json_obj(data)
    try:
        return int(payload.get("status") or 0)
    except Exception:
        return 0


def _quote_response_has_display_result(data: Any) -> bool:
    payload = _json_obj(_json_obj(data).get("data"))
    if not payload or not _has_text(payload.get("piccScore")):
        return False
    premium_keys = (
        "sumPremium",
        "totalPremium",
        "premiumTotal",
        "biPremium",
        "ciPremium",
        "sumPayTax",
        "thisPayTax",
        "quotationNo",
        "quotationId",
    )
    if any(_has_text(payload.get(key)) for key in premium_keys):
        return True
    item_rows = _json_obj(data).get("itemKindTempList")
    if not isinstance(item_rows, list):
        item_rows = payload.get("itemKindTempList")
    if not isinstance(item_rows, list):
        return False
    return any(_has_text(_json_obj(row).get("premium")) for row in item_rows)


def _platform_message(data: Any, default: str = "平台返回业务校验失败") -> str:
    payload = _json_obj(data)

    def candidates(value: Any, *, depth: int = 0):
        if depth > 4:
            return
        if isinstance(value, Mapping):
            for key in (
                "errorMsg",
                "errorMessage",
                "resultMessage",
                "resultMsg",
                "businessControlMsg",
                "businessMsg",
                "errorInfo",
                "errorTitle",
                "normalizeErrorMsg",
                "message",
                "msg",
                "detail",
                "reason",
                "statusText",
            ):
                item = value.get(key)
                if item not in (None, "", {}, []):
                    yield item
            for item in value.values():
                if isinstance(item, (Mapping, list, tuple)):
                    yield from candidates(item, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (Mapping, list, tuple)):
                    yield from candidates(item, depth=depth + 1)
                elif item not in (None, ""):
                    yield item
        elif value not in (None, ""):
            yield value

    for value in candidates(payload):
        text = _strip_platform_error_code(value)
        low = text.strip().lower()
        if not text:
            continue
        if low in {"success", "ok", "fail", "failed", "error", "错误", "错误信息", "业务逻辑异常"}:
            continue
        if re.fullmatch(r"[A-Z0-9_|:\-]+", text):
            continue
        return text
    return default


def _platform_notice_text(value: Any) -> str:
    text = html.unescape(_to_str(value)).strip()
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?[^>]+>", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _join_unique_platform_notice_parts(*values: Any) -> str:
    parts: List[str] = []
    compact_parts: List[str] = []
    for value in values:
        text = _platform_notice_text(value)
        compact = re.sub(r"\s+", "", text)
        if not compact:
            continue
        if any(compact in existing for existing in compact_parts):
            continue
        replacement_index = next((idx for idx, existing in enumerate(compact_parts) if existing in compact), None)
        if replacement_index is not None:
            parts[replacement_index] = text
            compact_parts[replacement_index] = compact
            continue
        parts.append(text)
        compact_parts.append(compact)
    return "\n".join(parts).strip()


def _platform_datetime_text(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2})(?::(\d{1,2}))?)?", text)
    if not match:
        return text
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    try:
        return datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H时%M分")
    except ValueError:
        return text


def _loose_platform_date_text(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    if not text:
        return ""
    for pattern in (
        r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})",
        r"(\d{4})(\d{2})(\d{2})",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            ).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _platform_current_day_from_response(data: Any) -> str:
    payload = _json_obj(_json_obj(data).get("data"))
    raw = _to_str(payload.get("currentTime") or payload.get("current_time") or payload.get("time")).strip()
    if raw:
        head = raw.split(",", 1)[0].strip()
        if re.fullmatch(r"\d{12,}", head):
            try:
                return datetime.fromtimestamp(int(head) / 1000, TZ_BJ).date().strftime("%Y-%m-%d")
            except Exception:
                pass
        day = _date_text(head)
        if day:
            return day
    for key in ("date", "currentDate", "current_date", "today"):
        day = _date_text(payload.get(key))
        if day:
            return day
    return ""


def _platform_next_quote_start_date_from_day(day: Any) -> str:
    text = _date_text(day)
    if not text:
        return _next_day_text()
    try:
        return (datetime.strptime(text, "%Y-%m-%d").date() + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return _next_day_text()


def _date_obj(value: Any) -> Optional[date]:
    text = _date_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _platform_effective_quote_date(value: Any, *, min_day: Any = None) -> str:
    day = _date_text(value)
    if not day:
        return ""
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return day
    # 人保 0 点起保不能早于当前时间；历史建议日按次日 0 点重报更稳。
    minimum = _date_obj(min_day) or (date.today() + timedelta(days=1))
    if parsed < minimum:
        parsed = minimum
    return parsed.strftime("%Y-%m-%d")


def _insurance_date_adjustment_target_day(
    form: Mapping[str, Any],
    vehicle: Mapping[str, Any],
    *,
    kind: str,
    target_day: Any,
) -> str:
    """Choose one safe date for every field belonging to the same insurance."""
    if kind not in {"bi", "ci"}:
        return ""

    if kind == "bi":
        current_values = (
            form.get("prpCmain.startDate"),
            vehicle.get("startDateBI"),
        )
    else:
        current_values = (
            form.get("prpCmain.startDateCI"),
            vehicle.get("startDateCI"),
        )

    candidates = [
        parsed
        for parsed in (
            _date_obj(target_day),
            *(_date_obj(value) for value in current_values),
        )
        if parsed is not None
    ]
    if not candidates:
        return ""
    # Old platform notices must never make a newer date go backwards. If the
    # two request sections disagree, use the latest valid value and synchronize
    # both sections before the retry.
    return max(candidates).strftime("%Y-%m-%d")


def _insurance_date_adjustment_needed(
    form: Mapping[str, Any],
    vehicle: Mapping[str, Any],
    *,
    kind: str,
    target_day: Any,
    target_hour: Any = "",
    target_minute: Any = "",
) -> bool:
    """Return whether the final request still needs its insurance dates synchronized."""
    target = _insurance_date_adjustment_target_day(
        form,
        vehicle,
        kind=kind,
        target_day=target_day,
    )
    if not target:
        return False

    if kind == "bi":
        current_values = (
            form.get("prpCmain.startDate"),
            vehicle.get("startDateBI"),
        )
    elif kind == "ci":
        current_values = (
            form.get("prpCmain.startDateCI"),
            vehicle.get("startDateCI"),
        )
    else:
        return False

    if any(_date_text(value) != target for value in current_values):
        return True
    hour_key = "prpCmain.starthourci" if kind == "ci" else "prpCmain.starthourbi"
    minute_key = "prpCmain.startminuteci" if kind == "ci" else "prpCmain.startminutebi"
    expected_hour, expected_minute = _period_time_texts(target_hour, target_minute)
    if _safe_int_local(form.get(hour_key), 0) != _safe_int_local(expected_hour, 0):
        return True
    if _safe_int_local(form.get(minute_key), 0) != _safe_int_local(expected_minute, 0):
        return True
    if kind == "ci":
        expected_end_date = _ci_end_date_text(target, expected_hour, expected_minute)
        expected_end_hour = "24" if expected_hour == "0" and expected_minute == "0" else expected_hour
        expected_end_minute = expected_minute
        return (
            _date_text(form.get("prpCmain.endDateCI")) != expected_end_date
            or _safe_int_local(form.get("prpCmain.endhourci"), 24) != _safe_int_local(expected_end_hour, 24)
            or _safe_int_local(form.get("prpCmain.endminuteci"), 0) != _safe_int_local(expected_end_minute, 0)
        )
    return False


def _insurance_date_notice_from_adjustment(adjustment: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "type": "insurance_date_adjust",
        "message": _to_str(adjustment.get("message")).strip(),
        "commercial_start_date": _date_text(adjustment.get("commercial_start_date")),
        "compulsory_start_date": _date_text(adjustment.get("compulsory_start_date")),
        "commercial_start_hour": _to_str(adjustment.get("commercial_start_hour")).strip(),
        "commercial_start_minute": _to_str(adjustment.get("commercial_start_minute")).strip(),
        "compulsory_start_hour": _to_str(adjustment.get("compulsory_start_hour")).strip(),
        "compulsory_start_minute": _to_str(adjustment.get("compulsory_start_minute")).strip(),
        "adjustment_kinds": [
            item
            for item in (adjustment.get("adjustment_kinds") if isinstance(adjustment.get("adjustment_kinds"), list) else [])
            if item in {"bi", "ci"}
        ],
        "source": adjustment.get("source") or "platform_prompt",
    }


def _emit_insurance_date_adjust_notice(callback: Any, adjustment: Mapping[str, Any]) -> bool:
    return _emit_platform_auto_notice(callback, _insurance_date_notice_from_adjustment(adjustment))


def _emit_platform_auto_notice(callback: Any, notice_any: Mapping[str, Any]) -> bool:
    if not callable(callback):
        return False
    notice = dict(_json_obj(notice_any))
    if not _to_str(notice.get("message")).strip():
        return False
    try:
        return bool(callback(dict(notice)))
    except Exception:
        return False


def _format_reinsure_items_prompt(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return ""
    lines: List[str] = []
    for index, raw in enumerate(items[:3], start=1):
        item = _json_obj(raw)
        if not item:
            continue
        advise_start = _first_text(item.get("adviseStartDate"))
        if advise_start:
            adjustment_kinds = _reinsure_adjustment_kinds(item)
            if adjustment_kinds == ["ci"]:
                coverage_label = "交强险"
            elif "bi" in adjustment_kinds and "ci" in adjustment_kinds:
                coverage_label = "商业险、交强险"
            else:
                coverage_label = "商业险"
            lines.extend(
                [
                    f"该车辆{coverage_label}保险期间与现存有效保单重复投保，",
                    f"系统建议将起保日期调整为{_platform_datetime_text(advise_start)}",
                    "请确认是否调整？与其重复投保的有效保单概要信息如下：",
                ]
            )
        else:
            lines.append("该车辆与现存有效保单存在重复投保，请核实保单概要信息：")
        if len(items) > 1:
            lines.append(f"重复投保记录{index}：")
        lines.append(f"重复投保单号：{_first_text(item.get('policyNo'), item.get('proposalNo'))}")
        lines.append(f"保险公司名称：{_first_text(item.get('insurerName'), item.get('insurerCode'))}")
        coverage_list = item.get("itemList") if isinstance(item.get("itemList"), list) else []
        if coverage_list:
            lines.append("险种信息： 同步起保日期与险种")
            for coverage_index, coverage in enumerate(coverage_list[:12], start=1):
                coverage_name = _first_text(_json_obj(coverage).get("coverageName"), _json_obj(coverage).get("coverageCode"))
                if coverage_name:
                    lines.append(f"{coverage_index} {coverage_name}")
        lines.append(f"号牌号码： {_first_text(item.get('licensePlateNo'), item.get('licenseNo'))}")
        lines.append(f"号牌种类代码： {_first_text(item.get('licensePlateType'), item.get('licenseType'))}")
        lines.append(f"号牌底色： {_first_text(item.get('licensePlateColorCode'), item.get('licenseColorCode'))}")
        lines.append(f"车架号/VIN码： {_first_text(item.get('vin'), item.get('vinNo'), item.get('frameNo'))}")
        lines.append(f"发动机号： {_first_text(item.get('engineNo'), item.get('engine'))}")
        lines.append(f"起保日期： {_first_text(item.get('effectiveDate'), item.get('startDate'))}")
        lines.append(f"终保日期： {_first_text(item.get('expireDate'), item.get('endDate'))}")
        lines.append(f"签单日期： {_first_text(item.get('billDate'), item.get('signDate'))}")
        if index < min(len(items), 3):
            lines.append("")
    return "\n".join(line for line in lines if line is not None).strip()


def _reinsure_adjustment_kinds(item: Mapping[str, Any]) -> List[str]:
    coverage_list = item.get("itemList") if isinstance(item.get("itemList"), list) else []
    kinds: List[str] = []
    for coverage_any in coverage_list:
        coverage = _json_obj(coverage_any)
        code = _to_str(_first_text(coverage.get("coverageRealCode"), coverage.get("coverageCode"))).strip()
        name = _to_str(_first_text(coverage.get("coverageName"), coverage.get("coverageCode"))).strip()
        if code == "051074" or "交强" in name:
            if "ci" not in kinds:
                kinds.append("ci")
        elif code.startswith("051") or "商业" in name or "机动车" in name or "三者" in name or "车上人员" in name:
            if "bi" not in kinds:
                kinds.append("bi")
    return kinds or ["bi"]


def _reinsure_suggested_start_dates(items: Any) -> Dict[str, str]:
    """Collect the latest platform suggestion separately for commercial and compulsory cover."""
    dates: Dict[str, str] = {}
    if not isinstance(items, list):
        return dates
    for raw in items:
        item = _json_obj(raw)
        # `effectiveDate` is historical policy context. Only the explicit
        # `adviseStartDate` field means the platform is asking to change dates.
        suggested_day = _date_text(item.get("adviseStartDate"))
        if not suggested_day:
            continue
        for kind in _reinsure_adjustment_kinds(item):
            current_day = _date_text(dates.get(kind))
            if not current_day or suggested_day > current_day:
                dates[kind] = suggested_day
    return dates


def _reinsure_suggested_start_datetimes(items: Any) -> Dict[str, Dict[str, str]]:
    """Collect suggested dates plus optional time from structured duplicate-insurance rows."""
    values: Dict[str, Dict[str, str]] = {}
    if not isinstance(items, list):
        return values
    for raw in items:
        item = _json_obj(raw)
        parts = _platform_datetime_parts(item.get("adviseStartDate"))
        suggested_day = parts.get("date")
        if not suggested_day:
            continue
        for kind in _reinsure_adjustment_kinds(item):
            current_day = _date_text(_json_obj(values.get(kind)).get("date"))
            if not current_day or suggested_day > current_day:
                values[kind] = parts
    return values


def _reinsure_notice_suggested_start_datetime(message: Any) -> Dict[str, str]:
    text = _platform_notice_text(message)
    if not text:
        return {}
    compact = re.sub(r"\s+", "", text)
    date_time_pattern = r"(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?(?:\d{1,2}(?:[:：时点]\d{1,2})?分?)?)"
    for pattern in (
        rf"(?:系统建议|平台建议|建议).{{0,40}}(?:起保日期|保险期间|起期).{{0,24}}(?:调整为|调整至|改为|改至|变更为|同步至|为){date_time_pattern}",
        rf"(?:起保日期|保险期间|起期).{{0,24}}(?:调整为|调整至|改为|改至|变更为|同步至|为){date_time_pattern}",
        rf"(?:调整为|调整至|改为|改至|变更为|同步至){date_time_pattern}",
    ):
        match = re.search(pattern, compact)
        if not match:
            continue
        parts = _platform_datetime_parts(match.group(1))
        if parts.get("date"):
            return parts
    return {}


def _insurance_date_error_adjustment_kinds(message: Any) -> List[str]:
    text = _platform_notice_text(message)
    compact = re.sub(r"\s+", "", text)
    kinds: List[str] = []
    period_words = r"(?:起保|起期|保险期间|保险期限|保险起期)"
    action_words = r"(?:当前时间|之前|不能|不可|不允许|请核对|请修改|修改保险|调整为|调整至|改为|改至|变更为|同步至|建议)"
    if re.search(rf"(?:商业险?|商业).{{0,24}}{period_words}.{{0,120}}{action_words}", compact):
        kinds.append("bi")
    if re.search(rf"(?:交强险?|交强).{{0,24}}{period_words}.{{0,120}}{action_words}", compact):
        kinds.append("ci")
    return kinds


def _reinsure_notice_suggested_start_date(message: Any) -> str:
    parts = _reinsure_notice_suggested_start_datetime(message)
    if parts.get("date"):
        return _to_str(parts.get("date")).strip()
    text = _platform_notice_text(message)
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    date_pattern = r"(\d{4}(?:[-/年月.]?\d{1,2}){2})"
    for pattern in (
        rf"(?:系统建议|平台建议|建议).{{0,40}}(?:起保日期|保险期间|起期).{{0,24}}(?:调整为|调整至|改为|改至|变更为|同步至|为){date_pattern}",
        rf"(?:起保日期|保险期间|起期).{{0,24}}(?:调整为|调整至|改为|改至|变更为|同步至|为){date_pattern}",
        rf"(?:调整为|调整至|改为|改至|变更为|同步至){date_pattern}",
    ):
        match = re.search(pattern, compact)
        if not match:
            continue
        day = _loose_platform_date_text(match.group(1))
        if day:
            return day
    return ""


def _reinsure_notice_adjustment_kinds(message: Any) -> List[str]:
    text = _platform_notice_text(message)
    compact = re.sub(r"\s+", "", text)
    if not compact or "重复投保" not in compact:
        return []
    if not _reinsure_notice_suggested_start_date(compact):
        return []
    kinds: List[str] = []
    if (
        re.search(r"(?:商业险?|商业).{0,24}(?:保险期间|起保|起期|重复投保)", compact)
        or re.search(r"(?:机动车|车损|三者|车上人员|医保外).{0,40}(?:险|责任|损失|服务)", compact)
    ):
        kinds.append("bi")
    if (
        re.search(r"(?:交强险?|交强).{0,24}(?:保险期间|起保|起期|重复投保)", compact)
        or "机动车交通事故责任强制保险" in compact
    ):
        kinds.append("ci")
    return kinds or ["bi"]


def _implicit_renewal_quote_hint(message: Any) -> str:
    text = _platform_notice_text(message)
    compact = re.sub(r"\s+", "", text)
    if not compact or "不符合续保条件" in compact:
        return ""
    if "已按照续保流程处理" in compact or "符合续保条件" in compact:
        return text
    return ""


def _implicit_renewal_quote_adjustment_from_response(
    platform_response: Any,
    message: Any,
) -> Dict[str, Any]:
    """Detect PICC's successful quote response that silently turned into renewal."""
    hint = _implicit_renewal_quote_hint(message)
    if not hint:
        return {}
    payload = _json_obj(_json_obj(platform_response).get("data"))
    commercial_parts = _platform_datetime_parts(payload.get("lastExpireDateBI"))
    compulsory_parts = _platform_datetime_parts(payload.get("lastExpireDateCI"))
    commercial_start = _to_str(commercial_parts.get("date")).strip()
    compulsory_start = _to_str(compulsory_parts.get("date")).strip()
    kinds: List[str] = []
    if commercial_start:
        kinds.append("bi")
    if compulsory_start:
        kinds.append("ci")
    if not kinds:
        return {}
    return {
        "message": hint,
        "commercial_start_date": commercial_start,
        "compulsory_start_date": compulsory_start,
        "commercial_start_hour": _to_str(commercial_parts.get("hour")).strip(),
        "commercial_start_minute": _to_str(commercial_parts.get("minute")).strip(),
        "compulsory_start_hour": _to_str(compulsory_parts.get("hour")).strip(),
        "compulsory_start_minute": _to_str(compulsory_parts.get("minute")).strip(),
        "adjustment_kinds": kinds,
        "source": "implicit_renewal_quote_hint",
    }


def _used_fuel_quote_platform_dialog(data: Any) -> Dict[str, Any]:
    payload = _json_obj(_json_obj(data).get("data"))
    if not payload:
        return {}
    notice = _platform_notice_text(
        _first_text(
            payload.get("normalizeErrorMsg"),
            payload.get("errorMsg"),
            payload.get("errorMessage"),
            payload.get("businessControlMsg"),
            payload.get("businessMsg"),
            payload.get("checkResult"),
        )
    )
    reinsure_items = payload.get("prpReInsureItems") if isinstance(payload.get("prpReInsureItems"), list) else []
    if not notice and not reinsure_items:
        return {}

    sections = [part for part in (notice, _format_reinsure_items_prompt(reinsure_items)) if part]
    message = "\n\n".join(sections).strip()
    if not message:
        return {}
    first_reinsure = _json_obj(reinsure_items[0]) if reinsure_items else {}
    suggested_dates = _reinsure_suggested_start_dates(reinsure_items)
    suggested_datetimes = _reinsure_suggested_start_datetimes(reinsure_items)
    # Parse only the original platform notice. `_format_reinsure_items_prompt`
    # may include historical effective dates for display and must not turn them
    # into a synthetic date-adjustment instruction.
    notice_suggested_date = _reinsure_notice_suggested_start_date(notice)
    notice_suggested_datetime = _reinsure_notice_suggested_start_datetime(notice)
    adjustment_kinds = list(suggested_dates.keys())
    if not adjustment_kinds and notice_suggested_date:
        adjustment_kinds = _reinsure_notice_adjustment_kinds(message)
        for kind in adjustment_kinds:
            suggested_dates[kind] = notice_suggested_date
            if notice_suggested_datetime.get("date"):
                suggested_datetimes[kind] = notice_suggested_datetime
    if not adjustment_kinds and first_reinsure:
        adjustment_kinds = _reinsure_adjustment_kinds(first_reinsure)
    # A reinsure row without any usable suggested/effective date is historical
    # context only, not an instruction to change today's quote dates.
    if not suggested_dates:
        adjustment_kinds = []
    # This parser only preserves what PICC returned. The actual date used for
    # the automatic retry is resolved later with PICC's current-time endpoint,
    # so a local machine clock can never alter the submitted request.
    return {
        "type": "notice",
        "subtype": "insurance_date_adjust" if adjustment_kinds else "quote_platform_notice",
        "title": "报价提示",
        "severity": "warning",
        "message": message,
        "confirm_required": False,
        "confirm_text": "确定",
        "cancel_text": "",
        "close_text": "关闭",
        "confirm_action": {},
        "raw_suggested_commercial_start_date": _date_text(suggested_dates.get("bi")) if "bi" in adjustment_kinds else "",
        "raw_suggested_compulsory_start_date": _date_text(suggested_dates.get("ci")) if "ci" in adjustment_kinds else "",
        "raw_suggested_commercial_start_hour": _to_str(_json_obj(suggested_datetimes.get("bi")).get("hour")).strip() if "bi" in adjustment_kinds else "",
        "raw_suggested_commercial_start_minute": _to_str(_json_obj(suggested_datetimes.get("bi")).get("minute")).strip() if "bi" in adjustment_kinds else "",
        "raw_suggested_compulsory_start_hour": _to_str(_json_obj(suggested_datetimes.get("ci")).get("hour")).strip() if "ci" in adjustment_kinds else "",
        "raw_suggested_compulsory_start_minute": _to_str(_json_obj(suggested_datetimes.get("ci")).get("minute")).strip() if "ci" in adjustment_kinds else "",
        # Kept for compatibility with archived runtime payloads. These remain
        # raw platform values and are not local-date-adjusted.
        "suggested_commercial_start_date": _date_text(suggested_dates.get("bi")) if "bi" in adjustment_kinds else "",
        "suggested_compulsory_start_date": _date_text(suggested_dates.get("ci")) if "ci" in adjustment_kinds else "",
        "adjustment_kinds": adjustment_kinds,
        "reinsure_items": reinsure_items[:3] if reinsure_items else [],
    }


def _platform_response_requires_insurance_date_adjustment(data: Any) -> bool:
    """Whether a quote response must enter the period-adjustment retry path."""
    dialog = _used_fuel_quote_platform_dialog(data)
    if _to_str(dialog.get("subtype")).strip().lower() != "insurance_date_adjust":
        return False
    kinds = dialog.get("adjustment_kinds")
    return any(_to_str(kind).strip() in {"bi", "ci"} for kind in (kinds if isinstance(kinds, list) else []))


def _platform_notice_auto_notice_from_dialog(dialog_any: Any) -> Dict[str, Any]:
    dialog = _json_obj(dialog_any)
    message = _to_str(dialog.get("message")).strip()
    if not message:
        return {}
    return {
        "type": "platform_notice",
        "subtype": _to_str(dialog.get("subtype")).strip() or "quote_platform_notice",
        "title": _first_text(dialog.get("title"), "报价提示"),
        "severity": _first_text(dialog.get("severity"), "warning"),
        "message": message,
        "source": "platform_prompt",
    }


def _platform_auto_notice_message_key(notice_any: Any) -> str:
    return re.sub(r"\s+", "", _to_str(_json_obj(notice_any).get("message")).strip())


def _append_unique_platform_auto_notice(
    notices: List[Dict[str, Any]],
    notice_any: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    item = dict(_json_obj(notice_any))
    message_key = _platform_auto_notice_message_key(item)
    if not message_key:
        return None
    for previous in notices:
        previous_key = _platform_auto_notice_message_key(previous)
        if previous_key and (message_key in previous_key or previous_key in message_key):
            return None
    notices.append(item)
    return item


def _remember_platform_notice_from_quote_response(
    notices: List[Dict[str, Any]],
    quote_response: Any,
    *,
    auto_notice_callback: Any = None,
) -> bool:
    platform_dialog = _used_fuel_quote_platform_dialog(quote_response)
    if not platform_dialog or _to_str(platform_dialog.get("subtype")).strip().lower() == "insurance_date_adjust":
        return False
    platform_notice = _platform_notice_auto_notice_from_dialog(platform_dialog)
    item = _append_unique_platform_auto_notice(notices, platform_notice)
    if item is None:
        return False
    emitted = _emit_platform_auto_notice(auto_notice_callback, item)
    if emitted:
        item["emitted_to_chat"] = True
    return True


def _platform_business_error_dialog(data: Any) -> Dict[str, Any]:
    payload = _json_obj(data)
    body = _json_obj(payload.get("data"))
    message = _platform_notice_text(
        _first_text(
            body.get("normalizeErrorMsg"),
            body.get("errorMsg"),
            body.get("errorMessage"),
            payload.get("normalizeErrorMsg"),
            payload.get("errorMsg"),
            payload.get("errorMessage"),
            payload.get("message"),
            payload.get("statusText"),
        )
    )
    if not message:
        return {}
    title = _first_text(body.get("errorTitle"), payload.get("errorTitle"), "错误信息")
    return {
        "type": "notice",
        "subtype": "quote_business_error",
        "title": title or "错误信息",
        "severity": "error",
        "message": message,
        "confirm_required": False,
        "confirm_text": "确定",
        "close_text": "确定",
    }


def _compact_platform_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                out["__truncated__"] = True
                break
            out[_to_str(key)] = _compact_platform_payload(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_compact_platform_payload(item, depth=depth + 1) for item in value[:20]]
    text = _to_str(value)
    return text[:1000] if len(text) > 1000 else value


def _platform_debug_payload(data: Any) -> Dict[str, Any]:
    payload = _json_obj(data)
    body = _json_obj(payload.get("data"))
    raw_message = _first_text(
        body.get("normalizeErrorMsg"),
        body.get("errorMsg"),
        body.get("errorMessage"),
        payload.get("normalizeErrorMsg"),
        payload.get("errorMsg"),
        payload.get("errorMessage"),
    )
    return {
        "status": payload.get("status"),
        "statusText": payload.get("statusText"),
        "errorTitle": _first_text(body.get("errorTitle"), payload.get("errorTitle")),
        "raw_message": _platform_notice_text(raw_message),
        "message": _platform_message(payload, ""),
        "response": _compact_platform_payload(payload),
    }


def _plan_rows_from_response(data: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if any(key in value for key in ("planCode", "planName", "planPremium", "planAmount")):
                rows.append(dict(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(_json_obj(_json_obj(data).get("data")).get("planInfoListMap"))
    return rows


def _pick_joint_sale_plan_by_premium(data: Any, premium: Any) -> Dict[str, Any]:
    target = _money(premium)
    matches = [
        row for row in _plan_rows_from_response(data)
        if _money(row.get("planPremium")) == target
    ]
    if not matches:
        return {}
    matches.sort(
        key=lambda row: (
            _money(row.get("planAmount")),
            _to_str(row.get("planName")),
            _to_str(row.get("planCode")),
        ),
        reverse=True,
    )
    selected = dict(matches[0])
    selected["_matchCount"] = len(matches)
    return selected


def _tujia_anshun_config(defaults: Mapping[str, Any]) -> Dict[str, Any]:
    premium = _money(_default_value(defaults, PRODUCT_TUJIA_ANSHUN_PREMIUM, "398"))
    if premium <= 0:
        return {
            "enabled": False,
            "premium": "0",
            "amount": "0",
            "reason": "途家安顺保费为0，按规则不查询保额",
        }
    return {
        "enabled": True,
        "premium": _clean_money_text(premium),
        "amount": "",
    }


def _tujia_anshun_from_request_body(request_body: Mapping[str, Any]) -> Dict[str, Any]:
    joint_sale = _json_obj(request_body.get("jointSaleForm"))
    return _json_obj(joint_sale.get("tujiaAnshun"))


def _duplicate_kind_name(kind_code: Any) -> str:
    code = _to_str(kind_code).strip()
    return PICC_KIND_NAME_BY_CODE.get(code, f"平台险别代码{code}" if code else "")


def _duplicate_insured_vin_warning(vin_no: Any, payload: Mapping[str, Any]) -> str:
    rows = payload.get("list")
    if not isinstance(rows, list) or not rows:
        return ""
    vin = _clean_vehicle_cert_value("vin", _first_text(vin_no, *(_json_obj(row).get("vinNo") for row in rows)))
    ci_period = ""
    bi_period = ""
    kind_names: List[str] = []
    seen_kinds = set()
    for raw in rows:
        row = _json_obj(raw)
        kind_list = row.get("kindCodeList")
        kind_codes = [
            _to_str(_json_obj(item).get("kindCode")).strip()
            for item in (kind_list if isinstance(kind_list, list) else [])
        ]
        period = ""
        if row.get("startDate") or row.get("endDate"):
            period = f"{_to_str(row.get('startDate')).strip()}至{_to_str(row.get('endDate')).strip()}"
        if "051074" in kind_codes:
            ci_period = ci_period or period
        else:
            bi_period = bi_period or period
        for code in kind_codes:
            name = _duplicate_kind_name(code)
            if name and name not in seen_kinds:
                kind_names.append(name)
                seen_kinds.add(name)
    lines = [
        "重复投保提示",
        "",
        f"车辆VIN:{vin}近期已在我司承保，请核实后进行报价，避免重复投保。"
        if vin
        else "该车辆近期已在我司承保，请核实后进行报价，避免重复投保。",
    ]
    if ci_period:
        lines.extend(["交强险保险期间:", ci_period])
    if bi_period:
        lines.extend(["商业险保险期间:", bi_period])
    if kind_names:
        lines.append("承保险别:")
        lines.extend(f"{idx}.{name}" for idx, name in enumerate(kind_names, start=1))
    return "\n".join(lines)


def _contains_duplicate_quote(data: Any) -> bool:
    raw = _to_str(data)
    compact = re.sub(r"\s+", "", raw)
    return bool(
        re.search(r"(重复投保|重复报价|已报价|已经报价|不能重复(?:报价|投保)|已在我司承保|近期已承保)", compact)
    )


def _contains_quota_full(data: Any) -> bool:
    raw = _to_str(data)
    return bool(re.search(r"(额度|次数|查询).{0,16}(用完|已满|满了|不足|超限|限制)", raw))


def _checked(value: Any, default: bool = True) -> bool:
    text = _to_str(value).strip().lower()
    if not text:
        return default
    if text in {"0", "false", "no", "n", "否", "不", "不选", "不勾选", "取消", "关闭", "去掉"}:
        return False
    if text in {"1", "true", "yes", "y", "是", "选", "勾选", "开启", "打开"}:
        return True
    return default


def _checked_flag_text(value: Any, default: bool = True) -> str:
    return "1" if _checked(value, default=default) else "0"


def _repair_code_enabled(defaults: Mapping[str, Any]) -> bool:
    return _checked(
        _field_value(defaults, "送修码启用", "使用送修码", "启用送修码", "groupCodeValidStatus", fallback="1"),
        default=True,
    )


def _repair_code_value(defaults: Mapping[str, Any]) -> str:
    if not _repair_code_enabled(defaults):
        return ""
    return _first_text(_field_value(defaults, "送修码", "送修码代码", "专管代码", "monopolyCode"))


def _repair_code_name(defaults: Mapping[str, Any]) -> str:
    if not _repair_code_enabled(defaults):
        return ""
    return _first_text(_field_value(defaults, "送修码名称", "送修码机构", "专管名称", "monopolyName"))


def _duplicate_quote_confirmed(quote_payload: Mapping[str, Any], request_body: Mapping[str, Any]) -> bool:
    payload = _json_obj(quote_payload)
    preflight = _json_obj(request_body.get("preflight"))
    candidates = (
        payload.get("confirm_duplicate_quote"),
        payload.get("confirmDuplicateQuote"),
        payload.get("duplicate_quote_confirmed"),
        payload.get("allow_duplicate_quote"),
        preflight.get("confirmDuplicateQuote"),
        preflight.get("duplicateQuoteConfirmed"),
        preflight.get("allowDuplicateQuote"),
    )
    return any(_checked(value, default=False) for value in candidates)


def _duplicate_quote_confirmation_payload(request_body: Mapping[str, Any]) -> Dict[str, Any]:
    prechecks = _json_obj(_json_obj(request_body.get("preflight")).get("quotePrechecks"))
    duplicate = _json_obj(prechecks.get("duplicateVin"))
    if _safe_int_local(duplicate.get("total"), 0) <= 0:
        return {}
    warning = _to_str(duplicate.get("warning") or duplicate.get("message")).strip()
    if not warning:
        warning = "平台提示该车辆近期已承保，请核实后再继续报价。"
    return {
        "business_status": "duplicate_quote_auto_continued",
        "error_code": "duplicate_quote_auto_continued",
        "duplicate_quote_warning": warning,
        "duplicateVin": duplicate,
        "request_body": request_body,
        "request_body_draft": request_body,
        "offline_request_body": True,
    }


def _duplicate_quote_auto_notice_from_confirmation_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    data = _json_obj(payload)
    duplicate = _json_obj(data.get("duplicateVin"))
    warning = _to_str(data.get("duplicate_quote_warning") or duplicate.get("warning") or duplicate.get("message")).strip()
    if not warning:
        return {}
    return {
        "type": "duplicate_quote_notice",
        "message": warning,
        "source": "duplicate_vin_precheck",
        "duplicateVin": duplicate,
    }


def _duplicate_quote_notice_from_success_dialog(
    dialog: Mapping[str, Any],
    *,
    has_period_auto_notice: bool,
    has_duplicate_precheck_notice: bool = False,
) -> Dict[str, Any]:
    """Keep duplicate-insurance text visible when a successful quote needs no further date change."""
    if has_period_auto_notice or has_duplicate_precheck_notice:
        return {}
    message = _platform_notice_text(_json_obj(dialog).get("message"))
    if "重复投保" not in re.sub(r"\s+", "", message):
        return {}
    return {
        "type": "duplicate_quote_notice",
        "message": message,
        "source": "quote_response_duplicate_insurance",
    }


def _vehicle_price(row: Mapping[str, Any]) -> Decimal:
    candidates = (
        row.get("purchasePrice"),
        row.get("priceP"),
        row.get("priceT"),
        row.get("price"),
        row.get("actualValue"),
    )
    return max((_money(value) for value in candidates), default=Decimal("0"))


def _vehicle_rows(data: Any) -> List[Dict[str, Any]]:
    payload = _json_obj(data)
    rows = payload.get("result")
    if not isinstance(rows, list):
        rows = _json_obj(payload.get("data")).get("list")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _compact_vehicle_compare_text(value: Any) -> str:
    return re.sub(r"\s+", "", _to_str(value).upper())


def _vehicle_row_haystack(row: Mapping[str, Any]) -> str:
    keys = (
        "vehicleName",
        "carName",
        "brandName",
        "vehicleBrand",
        "vehicleAlias",
        "VEHICLE_FGW_CODE",
        "vehicleFgwCode",
        "modelIdCode",
        "platModelCode",
        "vehicleModelCode",
        "vehicleId",
        "modelCode",
        "vehicleMaker",
        "manufacturer",
    )
    return _compact_vehicle_compare_text(" ".join(_to_str(row.get(key)) for key in keys))


_VEHICLE_MODEL_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<code>[A-Z][A-Z0-9_-]{3,})(?![A-Z0-9])",
    flags=re.IGNORECASE,
)


def _vehicle_model_code_candidates(*values: Any) -> List[str]:
    """Extract standalone alphanumeric model codes from OCR/model text."""
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        compact = re.sub(r"\s+", "", _to_str(value).strip()).upper()
        if not compact:
            continue
        for match in _VEHICLE_MODEL_CODE_RE.finditer(compact):
            code = match.group("code").strip("_-")
            if len(code) < 4 or not any(char.isdigit() for char in code):
                continue
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


def _vehicle_vin_model_code_candidates(value: Any) -> List[str]:
    """Extract the leading model/VDS code used by PICC from a 17-char VIN."""
    compact = re.sub(r"[^A-Z0-9]", "", _to_str(value).strip().upper())
    if len(compact) != 17 or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact):
        return []
    # PICC's vehicle catalogue commonly indexes Lexus/Toyota models by the
    # first eight VIN characters, e.g. JTHKR5BH3J2327186 -> JTHKR5BH.
    return [compact[:8]]


def _vehicle_leading_brand_from_model(value: Any) -> str:
    """Extract a Chinese brand prefix when OCR merged it with the model code."""
    compact = re.sub(r"\s+", "", _to_str(value).strip())
    if not compact:
        return ""
    match = re.match(r"^(?P<brand>[\u4e00-\u9fff]{2,24})牌?(?=[A-Z0-9])", compact, flags=re.IGNORECASE)
    return _vehicle_brand_prefix(match.group("brand")) if match else ""


def _vehicle_brand_hint(vehicle: Mapping[str, Any]) -> str:
    explicit = _vehicle_brand_prefix(vehicle.get("brandNameHint"))
    if explicit:
        return explicit
    return _vehicle_leading_brand_from_model(
        _first_text(vehicle.get("rawModelName"), vehicle.get("modelName"))
    )


def _vehicle_model_code_from_vehicle(vehicle: Mapping[str, Any]) -> str:
    codes = _vehicle_model_code_candidates(
        vehicle.get("rawModelName"),
        vehicle.get("vehicleFgwCode"),
        vehicle.get("modelName"),
    )
    codes.extend(_vehicle_vin_model_code_candidates(vehicle.get("vin")))
    if codes:
        return codes[0]
    return _compact_vehicle_compare_text(
        _first_text(vehicle.get("rawModelName"), vehicle.get("modelName"))
    )


def _vehicle_vin_prefix_is_model_code(vehicle: Mapping[str, Any], value: Any) -> bool:
    """Whether a model token is only the VDS prefix of this vehicle's VIN."""
    candidate = _compact_vehicle_compare_text(value)
    if not candidate:
        return False
    return candidate in {
        _compact_vehicle_compare_text(item)
        for item in _vehicle_vin_model_code_candidates(vehicle.get("vin"))
    }


_VEHICLE_TYPE_SUFFIXES = ("轿车", "客车", "货车", "越野车", "牵引车", "专项作业车", "摩托车", "挂车")
_VEHICLE_UNUSABLE_MODEL_HINTS = {
    "轿车",
    "小型轿车",
    "客车",
    "小型客车",
    "车辆",
    "汽车",
    "新能源车",
    "新能源",
    "燃油车",
    "纯电",
    "纯电动",
    "纯电动轿车",
    "插电式混合动力",
    "插电式混合动力轿车",
    "混合动力",
    "增程式",
    "增程式混合动力",
    "增程式混合动力轿车",
    "燃料电池",
    "汽油",
    "柴油",
}


def _vehicle_model_hint_is_usable(vehicle: Mapping[str, Any], value: Any) -> bool:
    """Reject generic labels and VIN prefixes as sales-model names."""
    text = re.sub(r"\s+", "", _to_str(value).strip()).strip("*")
    if not text or _vehicle_vin_prefix_is_model_code(vehicle, text):
        return False
    compact = _compact_vehicle_compare_text(text)
    if len(compact) == 17 and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact):
        return False
    # A merged OCR value such as "雷克萨斯JTHKR5BH" still only contains the
    # VIN family prefix. Do not let it masquerade as a confirmed sales model.
    if any(
        prefix and prefix in compact
        for prefix in _vehicle_vin_model_code_candidates(vehicle.get("vin"))
    ):
        return False
    if text in _VEHICLE_UNUSABLE_MODEL_HINTS:
        return False
    # Energy / body-type phrases must not be treated as sales models even when
    # OCR puts them in CarName (e.g. "纯电动轿车").
    if re.fullmatch(
        r"(?:纯电动|插电式|增程式|燃料电池)?(?:混合动力)?(?:轿车|客车|货车|越野车)?",
        text,
    ):
        return False
    return len(text) >= 2


def _vehicle_term_lacks_sales_specificity(term: Any, vehicle: Mapping[str, Any]) -> bool:
    """Whether a query term is only brand / body-type and unsafe to auto-accept."""
    compact = _compact_vehicle_compare_text(term)
    if not compact:
        return True
    if _vehicle_vin_prefix_is_model_code(vehicle, compact):
        return True
    brand = _compact_vehicle_compare_text(_vehicle_brand_hint(vehicle))
    if not brand:
        brand = _compact_vehicle_compare_text(
            _vehicle_leading_brand_from_model(_first_text(vehicle.get("rawModelName"), vehicle.get("modelName")))
        )
    raw = _compact_vehicle_compare_text(_first_text(vehicle.get("rawModelName"), vehicle.get("modelName")))
    suffix = _compact_vehicle_compare_text(_vehicle_model_suffix_from_type(vehicle.get("vehicleType")))
    broad = {item for item in (brand, raw, f"{brand}{suffix}" if brand and suffix else "", f"{raw}{suffix}" if raw and suffix else "") if item}
    if compact in broad:
        # Alphanumeric sales codes (CT200h / DFL7000…) are specific enough.
        if re.search(r"[A-Z]+[0-9]|[0-9]+[A-Z]", compact, flags=re.IGNORECASE):
            return False
        return True
    if any(prefix and prefix in compact for prefix in _vehicle_vin_model_code_candidates(vehicle.get("vin"))):
        return True
    return False


def _vehicle_query_resource_codes(
    *,
    profile: Optional[Mapping[str, Any]] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    vehicle: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """Resolve jyQuery resource codes: vehicle > defaults > profile > 0524."""
    vehicle_obj = _json_obj(vehicle)
    explicit: Any = None
    for item in (
        vehicle_obj.get("vehicleQueryResources"),
        _field_value(defaults or {}, "车型查询资源码", "vehicleQueryResources", "jyVehicleRequest.resources"),
        _profile_text(_json_obj(profile), "vehicle_query_resources"),
    ):
        if isinstance(item, (list, tuple)) and item:
            explicit = item
            break
        text = _to_str(item).strip()
        if text:
            explicit = text
            break
    raw_items: List[str] = []
    if isinstance(explicit, (list, tuple)):
        raw_items.extend(_to_str(item).strip() for item in explicit)
    elif explicit:
        raw_items.extend(part.strip() for part in re.split(r"[,，|;/\s]+", _to_str(explicit)) if part.strip())
    out: List[str] = []
    seen: set[str] = set()
    for item in raw_items or ["0524"]:
        code = re.sub(r"[^0-9A-Za-z]", "", _to_str(item).strip())
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out or ["0524"]


def _vehicle_query_match_meta(
    term: Any,
    vehicle: Mapping[str, Any],
    *,
    vin_correlated: bool = False,
) -> Dict[str, str]:
    text = _to_str(term).strip()
    if vin_correlated:
        return {
            "modelQueryMatched": f"{text}（VIN前缀关联）",
            "modelQueryMatchKind": "vin_correlated",
            "modelQueryMatchLabel": "VIN前缀关联",
        }
    hint = _to_str(vehicle.get("vehicleNameHint")).strip()
    compact_term = _compact_vehicle_compare_text(text)
    compact_hint = _compact_vehicle_compare_text(hint)
    if hint and compact_hint and (
        compact_term == compact_hint
        or compact_hint in compact_term
        or compact_term in compact_hint
    ):
        return {
            "modelQueryMatched": text,
            "modelQueryMatchKind": "sales_model",
            "modelQueryMatchLabel": "销售车型直查",
        }
    return {
        "modelQueryMatched": text,
        "modelQueryMatchKind": "catalogue_term",
        "modelQueryMatchLabel": "目录关键词",
    }


def _apply_vehicle_query_match_meta(
    vehicle: Any,
    term: Any,
    *,
    vin_correlated: bool = False,
    resources: Any = "",
) -> None:
    if not isinstance(vehicle, dict):
        return
    meta = _vehicle_query_match_meta(term, vehicle, vin_correlated=vin_correlated)
    vehicle.update(meta)
    if resources:
        vehicle["vehicleQueryResourcesUsed"] = _to_str(resources).strip()


def _vehicle_vin_prefix_match_score(row: Mapping[str, Any], vehicle: Mapping[str, Any]) -> int:
    """Score only rows whose platform catalogue carries this VIN's VDS code."""
    prefixes = {
        _compact_vehicle_compare_text(item)
        for item in _vehicle_vin_model_code_candidates(vehicle.get("vin"))
    }
    if not prefixes:
        return 0
    exact_fields = (
        "VEHICLE_FGW_CODE",
        "vehicleFgwCode",
        "modelIdCode",
        "platModelCode",
        "vehicleModelCode",
        "vehicleId",
        "modelCode",
        "searchCode",
        "vehicleName",
        "carName",
    )
    score = 0
    for key in exact_fields:
        value = _compact_vehicle_compare_text(row.get(key))
        if value in prefixes:
            score = max(score, 1000)
        elif any(prefix and prefix in value for prefix in prefixes):
            score = max(score, 500)
    return score


def _vehicle_rows_correlated_to_vin(rows: List[Dict[str, Any]], vehicle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Keep broad-search rows only when their platform codes identify this VIN family."""
    return [
        row
        for row in rows
        if _vehicle_vin_prefix_match_score(row, vehicle) > 0
    ]


def _vehicle_model_resolution_failure_message(
    vehicle: Mapping[str, Any],
    tried_terms: List[str],
) -> str:
    raw_model = _to_str(vehicle.get("rawModelName") or vehicle.get("modelName")).strip() or "-"
    vin_prefix = _first_text(*_vehicle_vin_model_code_candidates(vehicle.get("vin")))
    name_hint = _to_str(vehicle.get("vehicleNameHint")).strip()
    block_reason = _to_str(vehicle.get("modelQueryBlockReason")).strip()
    usable_hint = _vehicle_model_hint_is_usable(vehicle, name_hint)
    if block_reason in {"broad_brand_without_vin", "brand_only_without_vin_or_sales_model"}:
        return (
            f"车型名称【{raw_model}】仅识别到品牌级信息，"
            "平台返回了候选但缺少车架号关联或销售车型（如 CT200h），"
            "无法安全自动选定；请补充完整车架号，或在资料中填写销售车型/品牌型号后重试"
        )
    if vin_prefix and not usable_hint:
        return (
            f"车型名称【{raw_model}】仅识别到品牌或VIN前缀【{vin_prefix}】，"
            "未确认到可用于人保报价的销售车型；已尝试平台车型查询，"
            "请补充车辆品牌型号/车型名称后重试"
        )
    cleaned_tried = [item for item in tried_terms if _to_str(item).strip()]
    tried_text = f"，已尝试：{'、'.join(cleaned_tried[:8])}" if cleaned_tried else ""
    return f"车型名称【{raw_model}】未查询到可用车型配置{tried_text}"


def _vehicle_query_term_requires_vin_correlation(term: Any, vehicle: Mapping[str, Any]) -> bool:
    """Whether a broad brand/code term must be correlated before acceptance."""
    compact_term = _compact_vehicle_compare_text(term)
    if not compact_term:
        return True
    brand = _compact_vehicle_compare_text(_vehicle_brand_hint(vehicle))
    suffix = _compact_vehicle_compare_text(_vehicle_model_suffix_from_type(vehicle.get("vehicleType")))
    broad_terms = {item for item in (brand, f"{brand}{suffix}" if brand and suffix else "") if item}
    if compact_term in broad_terms:
        return True
    if _vehicle_vin_prefix_is_model_code(vehicle, compact_term):
        return True
    return any(
        prefix and prefix in compact_term
        for prefix in _vehicle_vin_model_code_candidates(vehicle.get("vin"))
    )


def _vehicle_candidate_score(row: Mapping[str, Any], vehicle: Mapping[str, Any]) -> int:
    haystack = _vehicle_row_haystack(row)
    model_codes = _vehicle_model_code_candidates(
        vehicle.get("rawModelName"),
        vehicle.get("vehicleFgwCode"),
        vehicle.get("modelName"),
    )
    model_codes.extend(_vehicle_vin_model_code_candidates(vehicle.get("vin")))
    model_code = _vehicle_model_code_from_vehicle(vehicle)
    brand = _compact_vehicle_compare_text(_vehicle_brand_hint(vehicle))
    name_hint_raw = _vehicle_name_hint(vehicle.get("vehicleNameHint"))
    name_hint = (
        _compact_vehicle_compare_text(name_hint_raw)
        if _vehicle_model_hint_is_usable(vehicle, name_hint_raw)
        else ""
    )
    score = 0
    score += _vehicle_vin_prefix_match_score(row, vehicle)
    if any(code in haystack for code in model_codes):
        score += 100
    elif model_code and model_code in haystack:
        score += 100
    if brand and brand in haystack:
        score += 20
    if name_hint and name_hint in haystack:
        score += 15
    energy_expected = bool(re.search(r"(纯电|电动|新能源|插电|混合动力|BEV|PHEV|EV|增程)", _compact_vehicle_compare_text(vehicle.get("energyModelSuffix"))))
    if energy_expected and (
        re.search(r"(纯电|电动|新能源|插电|混合动力|BEV|PHEV|EV|增程)", haystack)
        or _to_str(row.get("energyTypePlat")).strip() in {"1", "2", "3", "4"}
        or _to_str(row.get("vehicleFuelType")).strip().upper() in PICC_VEHICLE_FUEL_TYPE_TO_ENERGY_TYPE_PLAT
        or _to_str(row.get("isEnergyCar")).strip() in {"1", "true", "True"}
    ):
        score += 10
    return score


def _is_no_data_platform_response(data: Any) -> bool:
    raw = _to_str(data)
    return bool(re.search(r"(无数据返回|无数据|未查询到|没有查询到|暂无数据|没有数据)", raw))


def _is_vehicle_query_no_data_response(data: Any) -> bool:
    """PICC jyQuery.do uses status=-1/Fail for a normal no-match search."""
    payload = _json_obj(data)
    status = _to_str(payload.get("status")).strip()
    status_text = _to_str(payload.get("statusText")).strip().lower()
    if status == "-1" and status_text in {"fail", "failed", "no data"}:
        return True
    return _is_no_data_platform_response(data)


def _pick_highest_price_vehicle(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=_vehicle_price)


def _renewal_compare_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", _to_str(value).upper())


def _renewal_same_text(left: Any, right: Any) -> bool:
    lval = _renewal_compare_text(left)
    rval = _renewal_compare_text(right)
    return bool(lval and rval and lval == rval)


def _renewal_text_match_score(left: Any, right: Any, *, matched: int, mismatched: int) -> int:
    lval = _renewal_compare_text(left)
    rval = _renewal_compare_text(right)
    if not lval or not rval:
        return 0
    return matched if lval == rval else -mismatched


def _renewal_end_day(row: Mapping[str, Any]) -> Optional[date]:
    return _parse_date(row.get("end_date") or row.get("endDate"))


def _renewal_candidate_score(row: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> int:
    current = _json_obj(current)
    score = 0
    score += _renewal_text_match_score(
        row.get("license_no") or row.get("licenseNo"),
        current.get("plate_no") or current.get("license_no"),
        matched=100,
        mismatched=160,
    )
    score += _renewal_text_match_score(
        row.get("vin") or row.get("vinNo") or row.get("frameNo"),
        current.get("vin"),
        matched=120,
        mismatched=420,
    )
    score += _renewal_text_match_score(
        row.get("engine_no") or row.get("engineNo"),
        current.get("engine_no"),
        matched=90,
        mismatched=220,
    )
    row_license_type = _normalize_license_type_value(row.get("license_type") or row.get("licenseType"))
    current_license_type = _normalize_license_type_value(current.get("license_type") or current.get("licenseType"))
    if row_license_type and current_license_type and row_license_type == current_license_type:
        score += 50
    elif row_license_type and current_license_type:
        score -= 120
    if _to_str(row.get("renewal_or_copy_flag") or row.get("renewalOrCopyFlag")).strip() == "1":
        score += 30
    if _to_str(row.get("policy_no_encode") or row.get("policyNoEncode")).strip():
        score += 20
    if _to_str(row.get("relation_policy_no_encode") or row.get("relationPolicyNoEncode")).strip():
        score += 20
    if _to_str(row.get("risk_code") or row.get("riskCode")).strip().upper() == "DAA":
        score += 10

    end_day = _renewal_end_day(row)
    target_start = _parse_date(
        current.get("commercial_start_date")
        or current.get("compulsory_start_date")
        or current.get("start_date")
    )
    if end_day and target_start:
        score -= min(abs((end_day - (target_start - timedelta(days=1))).days), 365)
    return score


def _renewal_candidate_sort_value(row: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> tuple[int, int, int]:
    end_day = _renewal_end_day(row)
    end_ord = end_day.toordinal() if end_day else 0
    risk_code = _to_str(row.get("risk_code") or row.get("riskCode")).strip().upper()
    risk_rank = 2 if risk_code == "DAA" else 1 if risk_code == "DZA" else 0
    return _renewal_candidate_score(row, current), end_ord, risk_rank


def _renewal_candidate_selection_reason(row: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> str:
    current = _json_obj(current)
    reasons: List[str] = []
    if _renewal_same_text(row.get("license_no") or row.get("licenseNo"), current.get("plate_no") or current.get("license_no")):
        reasons.append("车牌一致")
    if _renewal_same_text(row.get("vin") or row.get("vinNo") or row.get("frameNo"), current.get("vin")):
        reasons.append("VIN一致")
    if _renewal_same_text(row.get("engine_no") or row.get("engineNo"), current.get("engine_no")):
        reasons.append("发动机号一致")
    row_license_type = _normalize_license_type_value(row.get("license_type") or row.get("licenseType"))
    current_license_type = _normalize_license_type_value(current.get("license_type") or current.get("licenseType"))
    if row_license_type and current_license_type and row_license_type == current_license_type:
        reasons.append("号牌种类一致")
    if _to_str(row.get("renewal_or_copy_flag") or row.get("renewalOrCopyFlag")).strip() == "1":
        reasons.append("平台标记可续保")
    return "，".join(reasons) or "按候选评分和终保日期选择"


def _pick_renewal_policy_candidate(rows: List[Dict[str, Any]], current: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=lambda row: _renewal_candidate_sort_value(row, current))


def _quote_loss_override_amount(quote_payload: Mapping[str, Any], defaults: Optional[Mapping[str, Any]] = None) -> Decimal:
    if defaults is not None and _product_excluded(defaults, PRODUCT_LOSS):
        return Decimal("0")
    normalized_data = _json_obj(_json_obj(quote_payload).get("normalized_data"))
    if _product_excluded({PRODUCT_EXCLUSIONS_KEY: normalized_data.get(PRODUCT_EXCLUSIONS_KEY)}, PRODUCT_LOSS):
        return Decimal("0")
    overrides = _json_obj(normalized_data.get("quote_field_overrides"))
    amount = _money(_default_value(overrides, PRODUCT_LOSS))
    return amount if amount > 0 else Decimal("0")


def _vehicle_selection_rule(explicit_loss_amount: Any = None) -> str:
    return (
        "model_brand_energy_match_then_loss_threshold_purchase_price"
        if _money(explicit_loss_amount) > 0
        else "model_brand_energy_match_then_lowest_purchase_price"
    )


def _vehicle_loss_threshold(explicit_loss_amount: Any = None) -> Decimal:
    loss_amount = _money(explicit_loss_amount)
    if loss_amount <= 0:
        return Decimal("0")
    return (loss_amount / Decimal("1.3")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pick_best_vehicle_candidate(
    rows: List[Dict[str, Any]],
    vehicle: Mapping[str, Any],
    *,
    explicit_loss_amount: Any = None,
) -> Dict[str, Any]:
    if not rows:
        return {}
    scored_rows = [(_vehicle_candidate_score(row, vehicle), index, row) for index, row in enumerate(rows)]
    best_score = max(score for score, _, _ in scored_rows)
    best_rows = [(index, row, _vehicle_price(row)) for score, index, row in scored_rows if score == best_score]
    priced_rows = [(index, row, price) for index, row, price in best_rows if price > 0]
    loss_threshold = _vehicle_loss_threshold(explicit_loss_amount)
    if loss_threshold > 0 and priced_rows:
        eligible = [(index, row, price) for index, row, price in priced_rows if price >= loss_threshold]
        if eligible:
            return min(eligible, key=lambda item: (item[2], item[0]))[1]
        return max(priced_rows, key=lambda item: (item[2], -item[0]))[1]
    if priced_rows:
        return min(priced_rows, key=lambda item: (item[2], item[0]))[1]
    return best_rows[0][1]


PICC_CAR_KIND_LABELS = {
    "A01": "客车",
    "A02": "货车",
    "A03": "挂车",
    "A04": "特种车",
    "A05": "摩托车",
    "A06": "拖拉机",
}

PICC_USE_NATURE_LABELS = {
    "21": "家庭自用汽车",
    "211": "家庭自用汽车",
    "212": "非营业企业客车",
    "213": "非营业机关客车",
    "220": "营业出租租赁",
    "230": "营业城市公交",
    "240": "营业公路客运",
    "250": "营业货运",
}


def _code_label(code: Any, labels: Mapping[str, str], fallback_label: str = "") -> str:
    text = _to_str(code).strip()
    if not text:
        return ""
    label = labels.get(text, fallback_label)
    return f"{text}-{label}" if label else text


def _proposal_money_yuan(value: Any, *, keep_decimal: bool = True, default: str = "-") -> str:
    if value in (None, ""):
        return default
    amount = _money(value)
    if amount == 0 and _to_str(value).strip() not in {"0", "0.0", "0.00"}:
        return default
    if keep_decimal:
        return f"{_money_text(amount)}元"
    return f"{_clean_money_text(amount)}元"


def _proposal_start_datetime(date_value: Any, hour_value: Any = "", minute_value: Any = "") -> str:
    day = _date_text(date_value)
    if not day:
        return ""
    if not _has_text(hour_value) and not _has_text(minute_value):
        return day
    hour = _safe_int_local(hour_value, 0)
    minute = _safe_int_local(minute_value, 0)
    return f"{day} {hour:02d}:{minute:02d}"


def _find_quote_response_date(data: Mapping[str, Any], *, kind: str) -> str:
    payload = _json_obj(data)
    if kind == "ci":
        exact_keys = (
            "startDateCI",
            "ciStartDate",
            "cistartDate",
            "compulsoryStartDate",
            "compulsory_start_date",
            "jqStartDate",
            "jqxStartDate",
            "startDateC",
        )
    else:
        exact_keys = (
            "startDateBI",
            "biStartDate",
            "bistartDate",
            "commercialStartDate",
            "commercial_start_date",
            "businessStartDate",
            "bizStartDate",
            "startDateB",
        )
    for key in exact_keys:
        day = _date_text(payload.get(key))
        if day:
            return day
    return ""


def _proposal_start_datetime_from_quote_response(
    data: Mapping[str, Any],
    form: Mapping[str, Any],
    *,
    kind: str,
) -> str:
    is_ci = kind == "ci"
    response_day = _find_quote_response_date(data, kind=kind)
    if response_day:
        return _proposal_start_datetime(
            response_day,
            _first_text(
                data.get("startHourCI" if is_ci else "startHourBI"),
                data.get("starthourci" if is_ci else "starthourbi"),
                form.get("prpCmain.starthourci" if is_ci else "prpCmain.starthourbi"),
            ),
            _first_text(
                data.get("startMinuteCI" if is_ci else "startMinuteBI"),
                data.get("startminuteci" if is_ci else "startminutebi"),
                form.get("prpCmain.startminuteci" if is_ci else "prpCmain.startminutebi"),
            ),
        )
    return _proposal_start_datetime(
        form.get("prpCmain.startDateCI" if is_ci else "prpCmain.startDate"),
        form.get("prpCmain.starthourci" if is_ci else "prpCmain.starthourbi"),
        form.get("prpCmain.startminuteci" if is_ci else "prpCmain.startminutebi"),
    )


def _proposal_claim_summary(data: Mapping[str, Any], claim_bi: Any, claim_ci: Any) -> str:
    bi_risk = _json_obj(data.get("carQuoteRiskItemBIRsp"))
    years_bi = _first_text(bi_risk.get("insureYears"), data.get("insureCarYears"), data.get("noDamYearsBI"), data.get("noDamageYears"))
    parts: List[str] = []
    if years_bi:
        parts.append(f"商业险连续承保年数{years_bi}年")
    if _has_text(claim_bi):
        parts.append(f"连续承保期间出险次数{_to_str(claim_bi).strip()}次")
    if _has_text(claim_ci):
        parts.append(f"交强险{_to_str(claim_ci).strip()}次")
    return "，".join(parts)


def _quote_form_kind_index(form: Mapping[str, Any], kind_code: str) -> Optional[int]:
    target = _to_str(kind_code).strip()
    if not target:
        return None
    for key, value in form.items():
        match = re.fullmatch(r"prpCitemKindVos\[(\d+)\]\.kindCode", _to_str(key).strip())
        if match and _to_str(value).strip() == target:
            return int(match.group(1))
    return None


def _quote_form_shared_main_limit(form: Mapping[str, Any]) -> Optional[bool]:
    index = _quote_form_kind_index(form, "051063")
    if index is None:
        return None
    value = form.get(f"prpCitemKindVos[{index}].sharedAmountFlag")
    if not _has_text(value):
        return None
    return _checked(value, default=False)


def _quote_form_kind_rows(form: Mapping[str, Any]) -> List[Dict[str, Any]]:
    indexes: set[int] = set()
    for key in form.keys():
        match = re.fullmatch(r"prpCitemKindVos\[(\d+)\]\.[A-Za-z0-9_]+", _to_str(key).strip())
        if match:
            indexes.add(int(match.group(1)))
    rows: List[Dict[str, Any]] = []
    for index in sorted(indexes):
        rows.append(
            {
                "index": index,
                "kind_code": _to_str(form.get(f"prpCitemKindVos[{index}].kindCode")).strip(),
                "kind_name": _to_str(form.get(f"prpCitemKindVos[{index}].kindName")).strip(),
                "choose_flag": form.get(f"prpCitemKindVos[{index}].chooseFlag"),
                "amount": form.get(f"prpCitemKindVos[{index}].amount"),
                "quantity": form.get(f"prpCitemKindVos[{index}].quantity"),
            }
        )
    return rows


def _is_dangerous_quote_form_override_key(key: Any) -> bool:
    text = _to_str(key).strip()
    return bool(re.fullmatch(r"prpCitemKindVos\[\d+\]\.(kindCode|kindName|chooseFlag|amount)", text))


def _quote_form_next_kind_index(form: Mapping[str, Any]) -> int:
    max_index = -1
    for key in form.keys():
        match = re.fullmatch(r"prpCitemKindVos\[(\d+)\]\.kindCode", _to_str(key).strip())
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1


PICC_KIND_FORM_ORDER = {
    "051074": 0,
    "051050": 1,
    "051051": 2,
    "051052": 3,
    "051053": 4,
    "051063": 5,
    "051064": 6,
    "051085": 7,
}


def _reorder_quote_form_kind_rows(form: Dict[str, Any]) -> bool:
    row_pattern = re.compile(r"prpCitemKindVos\[(\d+)\]\.(.+)")
    rows: Dict[int, Dict[str, Any]] = {}
    for key, value in list(form.items()):
        match = row_pattern.fullmatch(_to_str(key).strip())
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = value
    if not rows:
        return False

    old_indices = sorted(rows)
    ordered_indices = sorted(
        old_indices,
        key=lambda index: (
            PICC_KIND_FORM_ORDER.get(_to_str(rows[index].get("kindCode")).strip(), 1000),
            index,
        ),
    )
    index_map = {old_index: new_index for new_index, old_index in enumerate(ordered_indices)}
    if all(index_map[index] == index for index in old_indices):
        return False

    for key in list(form.keys()):
        if row_pattern.fullmatch(_to_str(key).strip()):
            form.pop(key, None)
    for old_index in ordered_indices:
        new_index = index_map[old_index]
        for suffix, value in rows[old_index].items():
            form[f"prpCitemKindVos[{new_index}].{suffix}"] = value
    return True


def _set_quote_form_kind_row(
    form: Dict[str, Any],
    *,
    kind_code: str,
    kind_name: str,
    amount: Any = None,
    quantity: Any = None,
    shared_amount_flag: Any = None,
) -> bool:
    index = _quote_form_kind_index(form, kind_code)
    changed = False
    if index is None:
        index = _quote_form_next_kind_index(form)
        form[f"prpCitemKindVos[{index}].kindCode"] = kind_code
        form[f"prpCitemKindVos[{index}].kindName"] = kind_name
        form[f"prpCitemKindVos[{index}].chooseFlag"] = "true"
        changed = True
    else:
        if _to_str(form.get(f"prpCitemKindVos[{index}].kindName")).strip() != kind_name:
            form[f"prpCitemKindVos[{index}].kindName"] = kind_name
            changed = True
        if _to_str(form.get(f"prpCitemKindVos[{index}].chooseFlag")).strip().lower() != "true":
            form[f"prpCitemKindVos[{index}].chooseFlag"] = "true"
            changed = True
    if amount is not None:
        amount_text = _to_str(amount).strip()
        if _to_str(form.get(f"prpCitemKindVos[{index}].amount")).strip() != amount_text:
            form[f"prpCitemKindVos[{index}].amount"] = amount_text
            changed = True
    if quantity is not None:
        quantity_text = _to_str(quantity).strip()
        if _to_str(form.get(f"prpCitemKindVos[{index}].quantity")).strip() != quantity_text:
            form[f"prpCitemKindVos[{index}].quantity"] = quantity_text
            changed = True
    if shared_amount_flag is not None:
        flag_text = _to_str(shared_amount_flag).strip()
        if _to_str(form.get(f"prpCitemKindVos[{index}].sharedAmountFlag")).strip() != flag_text:
            form[f"prpCitemKindVos[{index}].sharedAmountFlag"] = flag_text
            changed = True
    return changed


def _reinsure_items_include_kind(reinsure_items: Any, kind_code: str, *name_markers: str) -> bool:
    if not isinstance(reinsure_items, list):
        return False
    target = _to_str(kind_code).strip()
    markers = tuple(_to_str(marker).strip() for marker in name_markers if _to_str(marker).strip())
    for item_any in reinsure_items:
        item = _json_obj(item_any)
        coverage_list = item.get("itemList") if isinstance(item.get("itemList"), list) else []
        for coverage_any in coverage_list:
            coverage = _json_obj(coverage_any)
            code = _to_str(_first_text(coverage.get("coverageRealCode"), coverage.get("coverageCode"))).strip()
            name = _to_str(_first_text(coverage.get("coverageName"), coverage.get("coverageCode"))).strip()
            if code == target or any(marker in name for marker in markers):
                return True
    return False


def _normalize_platform_adjusted_quote_products(
    form: Dict[str, Any],
    defaults: Mapping[str, Any],
    profile: Mapping[str, Any],
    adjustment: Mapping[str, Any],
) -> bool:
    """Keep configured core cover amounts and optional add-ons after PICC's date/risk sync prompts."""

    changed = False
    exclusions = _product_exclusions(defaults)
    third_party_config = _profile_product_default(defaults, profile, PRODUCT_THIRD_PARTY, "300")
    third_party_wan = _wan_or_amount_to_wan_text(third_party_config, "300")
    shared_main_limit = _checked(_profile_product_default(defaults, profile, PRODUCT_SHARED_LIMIT, True), default=True)
    medical_third_amount = _wan_or_amount_to_amount(
        _profile_product_default(defaults, profile, PRODUCT_MEDICAL_THIRD, third_party_config),
        third_party_wan or "300",
    )
    if shared_main_limit:
        medical_third_amount = _wan_or_amount_to_amount(third_party_wan, third_party_wan or "300")

    core_specs = [
        (PRODUCT_THIRD_PARTY, "051051", PRODUCT_THIRD_PARTY, third_party_wan, None),
        (PRODUCT_DRIVER, "051052", PRODUCT_DRIVER, _wan_or_amount_to_amount(_profile_product_default(defaults, profile, PRODUCT_DRIVER, "2"), "2"), None),
        (PRODUCT_PASSENGER, "051053", PRODUCT_PASSENGER, _wan_or_amount_to_amount(_profile_product_default(defaults, profile, PRODUCT_PASSENGER, "2"), "2"), None),
        (PRODUCT_MEDICAL_THIRD, "051063", PRODUCT_MEDICAL_THIRD, medical_third_amount, "1" if shared_main_limit else "0"),
    ]
    for canonical_name, kind_code, kind_name, amount, shared_flag in core_specs:
        if _canonical_product_name(canonical_name) in exclusions:
            continue
        changed = _set_quote_form_kind_row(
            form,
            kind_code=kind_code,
            kind_name=kind_name,
            amount=amount,
            shared_amount_flag=shared_flag,
        ) or changed

    actual_value = _first_text(
        form.get("prpCitemCar.actualValue"),
        form.get("prpCitemCar.referenceActualValue"),
    )
    road_rescue_quantity = _safe_int_local(_default_value(defaults, PRODUCT_ROAD_RESCUE, ""), 0)
    if _canonical_product_name(PRODUCT_ROAD_RESCUE) not in exclusions and road_rescue_quantity > 0:
        changed = _set_quote_form_kind_row(
            form,
            kind_code="051064",
            kind_name=PRODUCT_ROAD_RESCUE,
            amount="",
            quantity=str(road_rescue_quantity),
        ) or changed
    external_grid_amount = _external_grid_amount(defaults, actual_value)
    if _canonical_product_name(PRODUCT_EXTERNAL_GRID) not in exclusions and _is_positive_amount(external_grid_amount):
        changed = _set_quote_form_kind_row(
            form,
            kind_code="051085",
            kind_name=PRODUCT_EXTERNAL_GRID,
            amount=external_grid_amount,
        ) or changed
    changed = _reorder_quote_form_kind_rows(form) or changed
    return changed


def _ensure_platform_success(data: Any, *, action: str) -> None:
    payload = _json_obj(data)
    if "status" in payload and int(payload.get("status") or 0) != 0:
        message = _platform_message(payload, f"{action}失败：平台返回异常")
        raise PiccBusinessRequestError(message or f"{action}失败：平台返回异常", action=action, platform_response=payload)


def _extract_quote_times_count(data: Any) -> Optional[int]:
    payload = _json_obj(data)
    candidates: List[Any] = [
        payload.get("cqpCounts"),
        _json_obj(payload.get("data")).get("cqpCounts"),
        _json_obj(_json_obj(payload.get("data")).get("data")).get("cqpCounts"),
    ]
    for value in candidates:
        if value is None or _to_str(value).strip() == "":
            continue
        try:
            return max(0, int(_money(value)))
        except Exception:
            continue
    return None


def _quote_times_payload(data: Any) -> Dict[str, Any]:
    status = _platform_status_code(data)
    status_text = _platform_message(data, "")
    if status == 0 and status_text.strip().lower() == "success":
        status_text = ""
    payload = {
        "available": status == 0,
        "today_used_count": _extract_quote_times_count(data),
        "platform_status": status,
        "platform_status_text": status_text,
        "source": "queryQuoteTimes",
        "queried_at": datetime.now().isoformat(timespec="seconds"),
    }
    if payload["today_used_count"] is None:
        payload["available"] = False
    return payload


def _actual_value_from_response(data: Any, fallback: Any = "") -> str:
    payload = _json_obj(data)
    nested = payload.get("data")
    candidates: List[Any] = []
    if isinstance(nested, Mapping):
        candidates.extend(
            nested.get(key)
            for key in (
                "actualValue",
                "actualVal",
                "actualvalue",
                "newActualValue",
                "calActualValue",
                "calActualVal",
                "value",
            )
        )
    else:
        candidates.append(nested)
    candidates.extend(
        payload.get(key)
        for key in (
            "actualValue",
            "actualVal",
            "actualvalue",
            "newActualValue",
            "calActualValue",
            "calActualVal",
            "value",
        )
    )
    candidates.append(fallback)
    for value in candidates:
        text = _to_str(value).strip()
        if text:
            return text
    return ""


def _model_search_code(value: Any) -> str:
    text = _to_str(value).strip().upper()
    match = re.search(r"[A-Z]{2,}[A-Z0-9]{4,}", text)
    return match.group(0) if match else text.replace("*", "")


def _first_positive_money_text(*values: Any, default: Any = "") -> str:
    for value in values:
        text = _to_str(value).strip()
        if not text:
            continue
        amount = _money(text)
        if amount > 0:
            return _clean_money_text(amount)
    return _to_str(default).strip()


def _vehicle_platform_purchase_price(selected: Mapping[str, Any], precise_vehicle: Mapping[str, Any]) -> str:
    return _first_positive_money_text(
        selected.get("purchasePrice"),
        selected.get("priceP"),
        selected.get("priceT"),
        precise_vehicle.get("purchasePrice"),
        precise_vehicle.get("priceP"),
        precise_vehicle.get("priceT"),
    )


def _energy_type_plat_from_vehicle_text(*values: Any) -> str:
    text = _compact_vehicle_compare_text(" ".join(_to_str(value) for value in values if _to_str(value).strip()))
    if not text:
        return ""
    if re.search(r"(插电|PHEV|PLUG.?IN)", text, flags=re.I):
        return "3"
    if re.search(r"(增程|EREV|REEV)", text, flags=re.I):
        return "4"
    if re.search(r"(燃料电池|氢能源|氢燃料)", text, flags=re.I):
        return "2"
    if re.search(r"(纯电|纯电动|BEV|ELECTRIC)", text, flags=re.I):
        return "1"
    if re.search(r"(汽油|柴油|燃油|GASOLINE|DIESEL)", text, flags=re.I):
        return "0"
    return ""


def _normalize_energy_type_plat(value: Any) -> str:
    text = _to_str(value).strip()
    return text if text in PICC_ENERGY_TYPE_PLAT_LABELS else ""


def _energy_type_plat_label(code: Any, *name_values: Any) -> str:
    code_text = _normalize_energy_type_plat(code)
    for value in name_values:
        text = _to_str(value).strip()
        if not text:
            continue
        inferred = _energy_type_plat_from_vehicle_text(text)
        if code_text and inferred == code_text:
            return PICC_ENERGY_TYPE_PLAT_LABELS.get(code_text, text)
        if not code_text and inferred:
            return PICC_ENERGY_TYPE_PLAT_LABELS.get(inferred, text)
        if code_text and code_text in PICC_ENERGY_TYPE_PLAT_LABELS:
            return PICC_ENERGY_TYPE_PLAT_LABELS[code_text]
        return text
    return PICC_ENERGY_TYPE_PLAT_LABELS.get(code_text, "")


def _vehicle_energy_model_suffix(data: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    text_values = (
        data.get("vehicle_energy_type"),
        data.get("energy_type"),
        data.get("fuel_type"),
        data.get("fuel_kind"),
        data.get("vehicle_model"),
        data.get("vehicle_brand_name"),
        data.get("vehicle_name"),
        data.get("vehicle_certificate_text"),
        data.get("generic_ocr_text"),
        data.get("ocr_text"),
        data.get("raw_text"),
    )
    inferred = _energy_type_plat_from_vehicle_text(*text_values)
    if inferred == "3":
        return "插电式混合动力"
    if inferred == "4":
        return "增程式混合动力"
    if inferred == "2":
        return "燃料电池"
    if inferred == "1" or _profile_text(profile, "is_energy_car") == "1":
        return "纯电动轿车"
    return ""


def _resolve_vehicle_energy_fields(
    defaults: Mapping[str, Any],
    selected: Mapping[str, Any],
    precise_vehicle: Mapping[str, Any],
    *,
    vehicle: Optional[Mapping[str, Any]] = None,
    profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    prof = _json_obj(profile)
    vehicle_obj = _json_obj(vehicle)
    selected_obj = _json_obj(selected)
    precise_obj = _json_obj(precise_vehicle)
    vehicle_fuel_type = _first_text(
        precise_obj.get("vehicleFuelType"),
        selected_obj.get("vehicleFuelType"),
        _field_value(defaults, "车辆燃料类型", "vehicleFuelType", fallback=_profile_text(prof, "vehicle_fuel_type", "D1")),
    )
    vehicle_code = _first_text(precise_obj.get("energyTypePlat"), selected_obj.get("energyTypePlat"))
    inferred_code = _first_text(
        PICC_VEHICLE_FUEL_TYPE_TO_ENERGY_TYPE_PLAT.get(_to_str(vehicle_fuel_type).strip().upper(), ""),
        PICC_PM_FUEL_TYPE_TO_ENERGY_TYPE_PLAT.get(_to_str(_first_text(precise_obj.get("pmFuelType"), selected_obj.get("pmFuelType"))).strip(), ""),
        _energy_type_plat_from_vehicle_text(
            precise_obj.get("fuel_type"),
            selected_obj.get("fuel_type"),
            precise_obj.get("vehicleName"),
            selected_obj.get("vehicleName"),
            precise_obj.get("carName"),
            selected_obj.get("carName"),
            vehicle_obj.get("selectedModelName"),
            vehicle_obj.get("modelName"),
            vehicle_obj.get("rawModelName"),
            vehicle_obj.get("energyModelSuffix"),
        ),
    )
    if _normalize_energy_type_plat(vehicle_code) == "0" and _normalize_energy_type_plat(inferred_code) not in {"", "0"}:
        code = inferred_code
    else:
        code = _first_text(
            vehicle_code,
            inferred_code,
            _field_value(defaults, "能源类型代码", "energyTypePlat"),
            _profile_text(prof, "energy_type_plat", "0"),
        )
    code = _normalize_energy_type_plat(code) or _profile_text(prof, "energy_type_plat", "0")
    name = _energy_type_plat_label(
        code,
        precise_obj.get("energyTypePlatTemp"),
        selected_obj.get("energyTypePlatTemp"),
        precise_obj.get("fuel_type"),
        selected_obj.get("fuel_type"),
        _field_value(defaults, "能源类型名称", "energyTypePlatTemp"),
        _profile_text(prof, "energy_type_name", ""),
    )
    is_energy_car = "1" if code != "0" or _checked(_first_text(precise_obj.get("isEnergyCar"), selected_obj.get("isEnergyCar")), default=False) else "0"
    energy_flag = "1" if is_energy_car == "1" else "0"
    return {
        "energy_type_plat": code,
        "energy_type_name": name or PICC_ENERGY_TYPE_PLAT_LABELS.get(code, ""),
        "vehicle_energy_type": code,
        "is_energy_car": is_energy_car,
        "energy_flag": energy_flag,
        "fuel_type": _to_str(_field_value(defaults, "燃料种类", "fuelType", fallback=_profile_text(prof, "fuel_type", "A"))),
        "vehicle_fuel_type": vehicle_fuel_type,
    }


def _profile_license_type(profile: Mapping[str, Any], energy_fields: Mapping[str, Any]) -> str:
    if _to_str(energy_fields.get("is_energy_car")).strip() == "1":
        return "52"
    return _profile_text(profile, "license_type", "02") or "02"


def _profile_license_color_code(profile: Mapping[str, Any], energy_fields: Mapping[str, Any]) -> str:
    if _to_str(energy_fields.get("is_energy_car")).strip() == "1":
        return "52"
    return _profile_text(profile, "license_color_code", "01") or "01"


def _normalize_license_type_value(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value)).upper()
    if not text:
        return ""
    if text in {"52", "新能源", "新能源车", "小型新能源汽车", "小型新能源汽车号牌", "绿色", "绿牌"}:
        return "52"
    if text in {"02", "油车", "燃油", "燃油车", "小型汽车", "小型汽车号牌", "蓝色", "蓝牌"}:
        return "02"
    if re.search(r"(?:新能源|绿牌|绿色|小型新能源)", text):
        return "52"
    if re.search(r"(?:燃油|油车|蓝牌|蓝色|小型汽车号牌|小型汽车)", text):
        return "02"
    return text if text in {"02", "52"} else ""


def _license_color_for_type(value: Any) -> str:
    license_type = _normalize_license_type_value(value)
    if license_type == "52":
        return "52"
    if license_type == "02":
        return "01"
    return ""


def _license_decision_fields(value: Any) -> Dict[str, str]:
    decision = _json_obj(value)
    if _to_str(decision.get("source")).strip() == "fallback":
        return {}
    license_type = _normalize_license_type_value(
        decision.get("license_type")
        or decision.get("licenseType")
        or decision.get("license_plate_type")
        or decision.get("licensePlateType")
    )
    if not license_type:
        return {}
    return {
        "license_type": license_type,
        "license_color_code": _first_text(
            _normalize_license_type_value(decision.get("license_color_code") or decision.get("licenseColorCode")),
            _license_color_for_type(license_type),
        ),
        "source": _to_str(decision.get("source")).strip(),
        "reason": _to_str(decision.get("reason")).strip(),
    }


def _profile_tax_defaults(
    profile: Mapping[str, Any],
    energy_fields: Mapping[str, Any],
    start_date: Any,
) -> Dict[str, str]:
    is_energy_car = _to_str(energy_fields.get("is_energy_car")).strip() == "1"
    if is_energy_car:
        start_day = _date_text(start_date)
        year = start_day[:4] if start_day else _today_text()[:4]
        return {
            "tax_type": _profile_text(profile, "tax_type", "2") or "2",
            "calculate_mode": _profile_text(profile, "tax_calculate_mode", "C1") or "C1",
            "tax_abate_type": _profile_text(profile, "tax_abate_type", "1") or "1",
            "tax_abate_reason": _profile_text(profile, "tax_abate_reason", "06") or "06",
            "duty_paid_proof_no": _profile_text(profile, "duty_paid_proof_no", "0012061001") or "0012061001",
            "pay_start_date": f"{year}-01-01",
            "pay_end_date": f"{year}-12-31",
        }
    return {
        "tax_type": _profile_text(profile, "tax_type", "1") or "1",
        "calculate_mode": _profile_text(profile, "tax_calculate_mode", "C1") or "C1",
        "tax_abate_type": _profile_text(profile, "tax_abate_type", "1") or "1",
        "tax_abate_reason": "",
        "duty_paid_proof_no": "",
        "pay_start_date": "",
        "pay_end_date": "",
    }


def _profile_license_fields(
    profile: Mapping[str, Any],
    energy_fields: Mapping[str, Any],
    defaults: Mapping[str, Any],
    vehicle: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    vehicle_obj = _json_obj(vehicle)
    decision_fields = _license_decision_fields(vehicle_obj.get("license_type_decision"))
    if decision_fields:
        return {
            "license_type": decision_fields["license_type"],
            "license_color_code": _first_text(
                decision_fields.get("license_color_code"),
                _license_color_for_type(decision_fields["license_type"]),
            ),
        }

    profile_license_type = _normalize_license_type_value(_profile_license_type(profile, energy_fields))
    profile_license_color_code = _profile_license_color_code(profile, energy_fields)
    default_license_type = _normalize_license_type_value(_field_value(defaults, "号牌种类", "licenseType"))
    default_license_color_code = _to_str(_field_value(defaults, "车牌颜色代码", "licenseColorCode")).strip()

    if default_license_type:
        return {
            "license_type": default_license_type,
            "license_color_code": _first_text(
                default_license_color_code,
                _license_color_for_type(default_license_type),
            ),
        }

    vehicle_license_type = _normalize_license_type_value(
        vehicle_obj.get("licenseType")
        or vehicle_obj.get("license_type")
    )
    if vehicle_license_type and (
        not profile_license_type
        or vehicle_license_type == profile_license_type
    ):
        return {
            "license_type": vehicle_license_type,
            "license_color_code": _first_text(
                _normalize_license_type_value(vehicle_obj.get("licenseColorCode") or vehicle_obj.get("license_color_code")),
                _license_color_for_type(vehicle_license_type),
            ),
        }
    return {
        "license_type": profile_license_type or "02",
        "license_color_code": _first_text(profile_license_color_code, _license_color_for_type(profile_license_type), "01"),
    }


def _profile_tax_field_values(
    profile: Mapping[str, Any],
    energy_fields: Mapping[str, Any],
    defaults: Mapping[str, Any],
    start_date: Any,
) -> Dict[str, str]:
    tax_defaults = _profile_tax_defaults(profile, energy_fields, start_date)
    return {
        "tax_type": _first_text(
            _field_value(defaults, "车船税类型", "taxType"),
            tax_defaults["tax_type"],
        ),
        "calculate_mode": _first_text(
            _field_value(defaults, "车船税计算方式", "calculateMode"),
            tax_defaults["calculate_mode"],
        ),
        "tax_abate_type": _first_text(
            _field_value(defaults, "车船税减免类型", "taxAbateType"),
            tax_defaults["tax_abate_type"],
        ),
        "tax_abate_reason": _first_text(
            _field_value(defaults, "车船税减免原因", "taxAbateReason"),
            tax_defaults["tax_abate_reason"],
        ),
        "duty_paid_proof_no": _first_text(
            _field_value(defaults, "完税证明号", "dutyPaidProofNo"),
            tax_defaults["duty_paid_proof_no"],
        ),
        "pay_start_date": _first_text(
            _date_text(_field_value(defaults, "车船税起始日期", "payStartDate")),
            tax_defaults["pay_start_date"],
        ),
        "pay_end_date": _first_text(
            _date_text(_field_value(defaults, "车船税终止日期", "payEndDate")),
            tax_defaults["pay_end_date"],
        ),
    }


def _vehicle_platform_brand_id(selected: Mapping[str, Any], precise_vehicle: Mapping[str, Any]) -> str:
    return _first_text(selected.get("brandId"), precise_vehicle.get("brandId"))


def _vehicle_platform_brand_id_new(selected: Mapping[str, Any], precise_vehicle: Mapping[str, Any], brand_id: Any) -> str:
    brand = _to_str(brand_id).strip()
    precise_brand = _to_str(precise_vehicle.get("brandId")).strip()
    selected_brand = _to_str(selected.get("brandId")).strip()
    selected_brand_new = _to_str(selected.get("brandIDNew")).strip()
    precise_brand_new = _to_str(precise_vehicle.get("brandIDNew")).strip()
    if precise_brand_new:
        return precise_brand_new
    if selected_brand_new and (not precise_brand or precise_brand == selected_brand or selected_brand_new.startswith(brand)):
        return selected_brand_new
    return f"{brand}0" if brand else ""


def _vehicle_platform_fgw_code(
    vehicle: Mapping[str, Any],
    selected: Mapping[str, Any],
    precise_vehicle: Mapping[str, Any],
) -> str:
    return _first_text(
        selected.get("VEHICLE_FGW_CODE"),
        selected.get("vehicleFgwCode"),
        vehicle.get("vehicleFgwCode"),
        precise_vehicle.get("VEHICLE_FGW_CODE"),
        precise_vehicle.get("vehicleFgwCode"),
        vehicle.get("modelName"),
    )


def _vehicle_platform_search_seqno(selected: Mapping[str, Any], brand_id: Any, vehicle_fgw_code: Any) -> str:
    search_code = _first_text(selected.get("searchCode"))
    if search_code:
        return search_code
    brand = _to_str(brand_id).strip()
    fgw = _to_str(vehicle_fgw_code).strip()
    return f"{brand}-{fgw}" if brand and fgw else fgw


def _vehicle_platform_mismatch_message(value: Any) -> bool:
    text = _to_str(value)
    return "CZACZBUA0001" in text or ("车型" in text and "平台返回" in text and "不一致" in text)


def _vehicle_platform_mismatch_codes(value: Any) -> List[str]:
    text = _to_str(value)
    out: List[str] = []
    for match in re.finditer(r"\b[A-Z][A-Z0-9]{6,}\d\b", text):
        code = match.group(0).strip().upper()
        if code.startswith(("KHYX", "HTTP")):
            continue
        if code not in out:
            out.append(code)
    return out


def _vehicle_row_codes(row: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("vehicleModelCode", "platModelCode", "modelIdCode", "vehicleId", "modelCode"):
        code = _to_str(row.get(key)).strip().upper()
        if code and code not in out:
            out.append(code)
    return out


def _accept_platform_returned_vehicle_body(request_body: Mapping[str, Any]) -> tuple[Dict[str, Any], bool]:
    body = _clean_used_fuel_request_body(request_body)
    form = dict(_json_obj(body.get("quoteForm")))
    preflight = dict(_json_obj(body.get("preflight")))
    vehicle = dict(_json_obj(body.get("vehicleForm")))
    selected = _json_obj(preflight.get("selectedVehicle"))
    precise_vehicle = _json_obj(preflight.get("preciseVehicle"))
    if not form or not precise_vehicle:
        return body, False

    changed = False

    def set_form(key: str, value: Any) -> None:
        nonlocal changed
        text = _to_str(value).strip()
        if not text:
            return
        if _to_str(form.get(key)).strip() != text:
            form[key] = text
            changed = True

    def set_vehicle(key: str, value: Any) -> None:
        nonlocal changed
        text = _to_str(value).strip()
        if not text:
            return
        if _to_str(vehicle.get(key)).strip() != text:
            vehicle[key] = text
            changed = True

    brand_id = _vehicle_platform_brand_id(selected, precise_vehicle)
    brand_id_new = _vehicle_platform_brand_id_new(selected, precise_vehicle, brand_id)
    vehicle_fgw_code = _vehicle_platform_fgw_code(vehicle, selected, precise_vehicle)
    purchase_price = _vehicle_platform_purchase_price(selected, precise_vehicle)
    model_code = _first_text(
        vehicle.get("platformModelCode"),
        precise_vehicle.get("vehicleId"),
        vehicle.get("modelCode"),
        selected.get("vehicleId"),
        selected.get("modelCode"),
    )
    vehicle_model_code = _first_text(
        vehicle.get("platformVehicleModelCode"),
        precise_vehicle.get("platModelCode"),
        selected.get("vehicleModelCode"),
        vehicle.get("platModelCode"),
    )
    selected_model_name = _first_text(precise_vehicle.get("vehicleName"), vehicle.get("selectedModelName"), selected.get("vehicleName"), vehicle.get("modelName"))
    profile = _motor_quote_profile(body.get("accountTypeName"))
    defaults = _json_obj(body.get("defaultFields"))
    energy_fields = _resolve_vehicle_energy_fields(defaults, selected, precise_vehicle, vehicle=vehicle, profile=profile)
    license_fields = _profile_license_fields(profile, energy_fields, defaults, vehicle=vehicle)
    tax_fields = _profile_tax_field_values(
        profile,
        energy_fields,
        defaults,
        _first_text(form.get("prpCmain.startDateCI"), form.get("prpCmain.startDate"), vehicle.get("startDateCI"), vehicle.get("startDateBI")),
    )
    is_new_car = bool(_profile_text(profile, "new_car_flag")) or _to_str(form.get("newCarFlag")).strip().lower() == "on"
    actual_value = _first_text(purchase_price if is_new_car else "", form.get("prpCitemCar.actualValue"), vehicle.get("actualValue"), purchase_price)

    set_form("prpCitemCar.brandName", selected_model_name)
    set_form("prpCitemCar.brandId", brand_id)
    set_form("prpCitemCar.brandIDNew", brand_id_new)
    set_form("prpCitemCar.familyId", _first_text(selected.get("familyId"), vehicle.get("familyId")))
    set_form("prpCitemCar.modelDemandNo", model_code)
    set_form("prpCitemCar.modelCode", model_code)
    set_form("prpCmain.vehicleModelCode", vehicle_model_code)
    set_form("prpCitemCar.vehicleFgwCode", vehicle_fgw_code)
    set_form("prpCitemCar.searchseqno", _vehicle_platform_search_seqno(selected, brand_id, vehicle_fgw_code))
    set_form("prpCitemCar.purchasePrice", purchase_price)
    set_form("prpCitemCar.actualValue", _money_text(actual_value))
    set_form("prpCitemCar.referenceActualValue", _money_text(actual_value))
    set_form("prpCitemCar.vehicleMaker", _first_text(selected.get("vehicleMaker"), precise_vehicle.get("vehicleMakerid"), precise_vehicle.get("vehicleMaker")))
    set_form("prpCitemCar.carLotEquQuality", _first_text(precise_vehicle.get("vehicleWeight"), selected.get("vehicleWeight")))
    set_form("prpCitemCar.enginePower", _first_text(precise_vehicle.get("enginePower"), selected.get("enginePower")))
    set_form("energyTypePlat", energy_fields["energy_type_plat"])
    set_form("energyTypePlatTemp", energy_fields["energy_type_name"])
    set_form("prpCitemCar.energyType", energy_fields["vehicle_energy_type"])
    set_form("prpCitemCar.isEnergyCar", energy_fields["is_energy_car"])
    set_form("energyFlag", energy_fields["energy_flag"])
    set_form("prpCitemCar.fuelType", energy_fields["fuel_type"])
    set_form("prpCitemCar.vehicleFuelType", energy_fields["vehicle_fuel_type"])
    set_form("prpCitemCar.licenseType", license_fields["license_type"])
    set_form("prpCitemCar.licenseColorCode", license_fields["license_color_code"])
    set_form("prpCcarShipTax.taxType", tax_fields["tax_type"])
    set_form("prpCcarShipTax.calculateMode", tax_fields["calculate_mode"])
    set_form("prpCcarShipTax.taxAbateType", tax_fields["tax_abate_type"])
    set_form("prpCcarShipTax.taxAbateReason", tax_fields["tax_abate_reason"])
    set_form("prpCcarShipTax.dutyPaidProofNo", tax_fields["duty_paid_proof_no"])
    set_form("prpCcarShipTax.payStartDate", tax_fields["pay_start_date"])
    set_form("prpCcarShipTax.payEndDate", tax_fields["pay_end_date"])
    set_form("prpCmain.vehicleStyleUniqueId", _first_text(selected.get("vehicleStyleUniqueId"), vehicle.get("vehicleStyleUniqueId")))
    set_form("prpCmain.presaleCarFlag", _first_text(precise_vehicle.get("presaleCarFlag"), selected.get("presaleCarFlag")))

    if not _product_excluded(defaults, PRODUCT_LOSS) and not _to_str(_default_value(defaults, PRODUCT_LOSS)).strip():
        loss_index = _quote_form_kind_index(form, "051050")
        if loss_index is not None:
            set_form(f"prpCitemKindVos[{loss_index}].amount", _money_text(actual_value))

    set_vehicle("purchasePrice", purchase_price)
    set_vehicle("actualValue", _money_text(actual_value))
    set_vehicle("modelCode", model_code)
    set_vehicle("platModelCode", vehicle_model_code)
    set_vehicle("selectedModelName", selected_model_name)
    set_vehicle("selectedVehicleId", model_code)
    set_vehicle("vehicleFgwCode", _model_search_code(vehicle_fgw_code))
    set_vehicle("platformBrandId", brand_id)
    set_vehicle("platformBrandIDNew", brand_id_new)
    set_vehicle("licenseType", license_fields["license_type"])
    set_vehicle("licenseColorCode", license_fields["license_color_code"])
    set_vehicle("taxType", tax_fields["tax_type"])
    set_vehicle("calculateMode", tax_fields["calculate_mode"])
    set_vehicle("taxAbateType", tax_fields["tax_abate_type"])
    set_vehicle("taxAbateReason", tax_fields["tax_abate_reason"])
    set_vehicle("dutyPaidProofNo", tax_fields["duty_paid_proof_no"])
    set_vehicle("payStartDate", tax_fields["pay_start_date"])
    set_vehicle("payEndDate", tax_fields["pay_end_date"])

    preflight["vehicleModelAutoAccepted"] = {
        "accepted": True,
        "reason": "平台提示车型不一致，已自动使用精确车型确认接口返回值重试一次",
        "brandId": brand_id,
        "vehicleName": selected_model_name,
        "vehicleId": model_code,
        "vehicleModelCode": vehicle_model_code,
        "purchasePrice": purchase_price,
        "energyTypePlat": energy_fields["energy_type_plat"],
        "energyTypePlatTemp": energy_fields["energy_type_name"],
        "vehicleFuelType": energy_fields["vehicle_fuel_type"],
        "licenseType": license_fields["license_type"],
        "licenseColorCode": license_fields["license_color_code"],
        "taxType": tax_fields["tax_type"],
        "calculateMode": tax_fields["calculate_mode"],
        "taxAbateType": tax_fields["tax_abate_type"],
        "taxAbateReason": tax_fields["tax_abate_reason"],
    }
    body["quoteForm"] = _clean_vehicle_cert_fields(form)
    body["vehicleForm"] = _clean_vehicle_cert_fields(vehicle)
    body["preflight"] = preflight
    return _clean_used_fuel_request_body(body), changed


def _vehicle_model_suffix_from_type(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value))
    for suffix in _VEHICLE_TYPE_SUFFIXES:
        if suffix in text:
            return suffix
    return ""


def _vehicle_brand_prefix(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    if not text:
        return ""
    # OCR usually returns values such as "长安牌"; the platform search box works better without "牌".
    text = re.sub(r"(品牌|车辆品牌|车辆名称|车辆品牌/车辆名称)", "", text)
    text = re.sub(r"牌$", "", text)
    # Strip body-type tails so "雷克萨斯轿车" does not become a fake brand and
    # later concatenate into "雷克萨斯轿车雷克萨斯".
    for suffix in _VEHICLE_TYPE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip()


def _vehicle_name_hint(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    if not text:
        return ""
    return text


def _join_model_term(prefix: str, base: str, suffix: str = "") -> str:
    term = re.sub(r"\s+", "", _to_str(base).strip()).strip("*")
    if not term:
        return ""
    if prefix and not term.startswith(prefix):
        term = f"{prefix}{term}"
    if suffix and suffix not in term:
        term = f"{term}{suffix}"
    return term


def _dedupe_model_terms(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", _to_str(value).strip()).strip("*")
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


_ENGLISH_MODEL_BRAND_PREFIXES: Tuple[str, ...] = (
    "MERCEDESBENZ",
    "LANDROVER",
    "VOLKSWAGEN",
    "CHEVROLET",
    "MITSUBISHI",
    "CADILLAC",
    "PORSCHE",
    "HYUNDAI",
    "LEXUS",
    "TOYOTA",
    "HONDA",
    "NISSAN",
    "BUICK",
    "MAZDA",
    "TESLA",
    "VOLVO",
    "AUDI",
    "BENZ",
    "FORD",
    "JEEP",
    "MINI",
    "BMW",
    "KIA",
)


def _english_model_code_terms(value: Any) -> List[str]:
    """PICC model search is sensitive to spaces between English brand and model code."""

    source = re.sub(r"\s+", " ", _to_str(value).strip()).strip("*")
    if not source:
        return []
    compact = re.sub(r"\s+", "", source)
    out: List[str] = []

    def add(term: Any) -> None:
        text = re.sub(r"\s+", " ", _to_str(term).strip()).strip("*")
        if text:
            out.append(text)

    if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{0,5}\d[A-Za-z0-9]*", source):
        add(source)

    brand_group = "|".join(re.escape(item) for item in _ENGLISH_MODEL_BRAND_PREFIXES)
    pattern = re.compile(
        rf"(?P<prefix>[\u4e00-\u9fff]*)(?P<brand>{brand_group})(?P<model>[A-Za-z]{{0,5}}\d[A-Za-z0-9]*)(?P<suffix>[\u4e00-\u9fff]*)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(compact):
        prefix = match.group("prefix") or ""
        brand = match.group("brand") or ""
        model = match.group("model") or ""
        suffix = match.group("suffix") or ""
        if not brand or not model:
            continue
        spaced = f"{brand} {model}"
        add(f"{prefix}{spaced}{suffix}")
        add(f"{prefix}{spaced}")
        add(f"{spaced}{suffix}")
        add(spaced)
        add(f"{model}{suffix}")
        add(model)
    return _dedupe_model_terms(out)


def _used_fuel_model_query_terms(
    model_name: Any,
    vehicle_type: Any = "",
    energy_model_suffix: Any = "",
    *,
    brand_name: Any = "",
    vehicle_name: Any = "",
    vin: Any = "",
) -> List[str]:
    source = re.sub(r"\s+", " ", _to_str(model_name).strip()).strip("*")
    raw = re.sub(r"\s+", "", source).strip("*")
    if not raw:
        return []
    no_brand_suffix = re.sub(r"(?<=[\u4e00-\u9fff])牌(?=[A-Z0-9])", "", raw)
    suffix = _vehicle_model_suffix_from_type(vehicle_type)
    has_vehicle_suffix = bool(re.search(r"(轿车|客车|货车|越野车|牵引车|专项作业车|摩托车|挂车)$", no_brand_suffix))
    typed = f"{no_brand_suffix}{suffix}" if suffix and not has_vehicle_suffix else no_brand_suffix
    energy_suffix = _to_str(energy_model_suffix).strip()
    energy_typed = ""
    if energy_suffix and not re.search(r"(纯电动|插电式|混合动力|新能源)", no_brand_suffix):
        energy_base = re.sub(r"(轿车|客车|货车|越野车|牵引车|专项作业车|摩托车|挂车)$", "", no_brand_suffix)
        energy_typed = f"{energy_base}{energy_suffix}"
    brand = _vehicle_brand_prefix(brand_name) or _vehicle_leading_brand_from_model(raw)
    energy_branded = _join_model_term(brand, energy_typed) if energy_typed and brand else ""
    name_hint = _vehicle_name_hint(vehicle_name)
    # Never fall energy suffixes through as sales-model hints.
    usable_name_hint = _vehicle_model_hint_is_usable(
        {
            "vin": vin,
        },
        name_hint,
    )
    name_hint = name_hint if usable_name_hint else ""
    brand_name_hint = _join_model_term(brand, name_hint)
    brand_typed = _join_model_term(brand, no_brand_suffix, name_hint)
    brand_plain = _join_model_term(brand, no_brand_suffix)
    named_plain = _join_model_term("", no_brand_suffix, name_hint)
    english_terms = _english_model_code_terms(source)
    exact_model_terms: List[str] = []
    for model_code in _vehicle_model_code_candidates(source, vehicle_name):
        if _vehicle_vin_prefix_is_model_code({"vin": vin}, model_code):
            continue
        exact_model_terms.extend((model_code, _join_model_term("", model_code, suffix)))
    exact_model_terms = _dedupe_model_terms(exact_model_terms)
    broad_terms = _dedupe_model_terms(
        [
            brand_name_hint,
            name_hint,
            *english_terms,
            brand_typed,
            brand_plain,
            named_plain,
            energy_branded,
            energy_typed,
            typed,
            no_brand_suffix,
            raw,
        ]
    )
    return _dedupe_model_terms(
        [
            *exact_model_terms,
            *broad_terms,
            # Keep the VIN prefix as a final diagnostic query only. It is not
            # treated as a sales model and must never be converted to a
            # guessed model name.
            *_vehicle_vin_model_code_candidates(vin),
        ]
    )


def _vehicle_seed_identifier_matches_current(vehicle: Mapping[str, Any], seed: Mapping[str, Any]) -> bool:
    """Only trust cached/pre-filled vehicle model hints when they are for this car."""

    current_vin = _clean_vehicle_cert_value(
        "vin",
        _first_text(vehicle.get("vin"), vehicle.get("vinNo"), vehicle.get("frameNo")),
    )
    seed_vin = _clean_vehicle_cert_value(
        "vin",
        _first_text(seed.get("vin"), seed.get("vinNo"), seed.get("frameNo")),
    )
    current_engine = _clean_vehicle_cert_value("engine_no", _first_text(vehicle.get("engineNo"), vehicle.get("engine_no")))
    seed_engine = _clean_vehicle_cert_value("engine_no", _first_text(seed.get("engineNo"), seed.get("engine_no")))
    current_plate = _clean_vehicle_cert_value("plate_no", _first_text(vehicle.get("licenseNo"), vehicle.get("plate_no")))
    seed_plate = _clean_vehicle_cert_value("plate_no", _first_text(seed.get("licenseNo"), seed.get("plate_no")))

    matched = False
    for current, incoming in ((current_vin, seed_vin), (current_engine, seed_engine), (current_plate, seed_plate)):
        current_text = _to_str(current).strip().upper()
        incoming_text = _to_str(incoming).strip().upper()
        if current_text and incoming_text:
            if current_text != incoming_text:
                return False
            matched = True
    return matched


def _vehicle_model_seed_terms(vehicle: Mapping[str, Any], *seeds: Mapping[str, Any]) -> List[str]:
    """Extract safe sales-model query terms from renewal/history request seeds."""

    out: List[str] = []
    for raw_seed in seeds:
        seed = _json_obj(raw_seed)
        if not seed or not _vehicle_seed_identifier_matches_current(vehicle, seed):
            continue
        seed_brand_raw = _first_text(seed.get("brandName"), seed.get("brandNameHint"), vehicle.get("brandNameHint"))
        seed_brand = _vehicle_leading_brand_from_model(seed_brand_raw) or _vehicle_brand_prefix(seed_brand_raw)
        raw_values: List[Any] = [
            seed.get("selectedModelName"),
            seed.get("rawModelName"),
            seed.get("modelName"),
            seed.get("modelQueryMatched"),
            seed.get("vehicleFgwCode"),
        ]
        raw_values.extend(seed.get("modelQueryTerms") if isinstance(seed.get("modelQueryTerms"), list) else [])
        for value in raw_values:
            direct = _to_str(value).strip().strip("*")
            candidates = _dedupe_model_terms(
                [
                    direct,
                    *_used_fuel_model_query_terms(
                        direct,
                        vehicle.get("vehicleType"),
                        vehicle.get("energyModelSuffix"),
                        brand_name=seed_brand,
                        vehicle_name="",
                        vin=vehicle.get("vin"),
                    ),
                ]
            )
            for term in candidates:
                if not _vehicle_model_hint_is_usable(vehicle, term):
                    continue
                if _vehicle_term_lacks_sales_specificity(term, {**_json_obj(vehicle), "brandNameHint": seed_brand}):
                    continue
                out.append(term)
    return _dedupe_model_terms(out)


def _apply_vehicle_model_seed_hints(vehicle: Dict[str, Any], *seeds: Mapping[str, Any]) -> None:
    """Promote trusted renewal/history model hints without accepting stale broad terms."""

    terms = _vehicle_model_seed_terms(vehicle, *seeds)
    if not terms:
        return
    vehicle["trustedModelSeedTerms"] = terms
    first_term = terms[0]
    if not _vehicle_model_hint_is_usable(vehicle, vehicle.get("vehicleNameHint")):
        vehicle["vehicleNameHint"] = first_term
    if not _vehicle_model_hint_is_usable(vehicle, vehicle.get("rawModelName")):
        vehicle["rawModelName"] = first_term
        vehicle["modelName"] = first_term


def _normalize_used_fuel_model_name(model_name: Any, vehicle_type: Any = "") -> str:
    terms = _used_fuel_model_query_terms(model_name, vehicle_type)
    return terms[0] if terms else _to_str(model_name).strip()


def _new_car_placeholder_license(engine_no: Any, vin: Any) -> str:
    for value in (engine_no, vin):
        text = re.sub(r"[^A-Z0-9]", "", _to_str(value).upper())
        if len(text) >= 6:
            return text[-6:]
    return ""


class PiccBusinessAdapter(QuotePlatformAdapter):
    platform_code = "PICC"
    platform_name = "人保"
    requires_browser_runtime = False
    keep_browser_alive = False

    async def keepalive(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._keepalive_sync, ctx)

    async def check_quota(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._check_quota_sync, ctx)

    async def quote(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._quote_sync, ctx, quote_payload)

    async def query_renewal(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._query_renewal_sync, ctx, quote_payload)

    async def query_joint_sales_plan(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._query_joint_sales_plan_sync, ctx, quote_payload)

    async def query_repair_codes(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._query_repair_codes_sync, ctx, quote_payload)

    def _client(self, ctx: PlatformAccountContext) -> PiccProtocolClient:
        snapshot = snapshot_from_context(ctx)
        if snapshot is None:
            raise PiccSessionExpiredError("PICC 当前账号没有可用会话，请先登录")
        return PiccProtocolClient(ctx, snapshot=snapshot)

    def _keepalive_sync(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        try:
            client = self._client(ctx)
            data = client.keepalive()
            if isinstance(data, Mapping) and int(data.get("status", -1)) != 0:
                return PlatformRuntimeResult(
                    status="failed",
                    message=str(data.get("statusText") or data),
                    data=success_data(
                        client,
                        extra={
                            "business_status": data.get("status"),
                            "platform_status_text": data.get("statusText") or "",
                        },
                    ),
                )
            return PlatformRuntimeResult(
                status="success",
                message="PICC 保活成功",
                data=success_data(
                    client,
                    extra={
                        "platform_status": data.get("status") if isinstance(data, Mapping) else None,
                        "platform_status_text": data.get("statusText") if isinstance(data, Mapping) else "",
                    },
                ),
            )
        except PiccSessionExpiredError as exc:
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 登录已过期，请重新登录",
                data={"business_status": "16", "error_code": exc.__class__.__name__},
            )
        except PiccTransientGatewayError as exc:
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data={"error_code": exc.__class__.__name__, "transient": True},
            )
        except Exception as exc:
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={
                    "error_code": exc.__class__.__name__,
                },
            )

    def _check_quota_sync(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        result = self._keepalive_sync(ctx)
        if result.status in {"success", "ok"}:
            usage = self._query_quote_times_best_effort(ctx)
            return PlatformRuntimeResult(
                status="available",
                message="PICC 账号会话可用；业务额度以鼎昌系统配置为准",
                data={
                    **_json_obj(result.data),
                    "platform_usage": usage,
                },
            )
        return result

    def _renewal_lookup_vehicle(
        self,
        ctx: PlatformAccountContext,
        quote_payload: Mapping[str, Any],
    ) -> Dict[str, str]:
        payload = _json_obj(quote_payload)
        normalized = _clean_vehicle_cert_fields(_json_obj(payload.get("normalized_data")))
        detected = _json_obj(payload.get("vehicle_type_detect"))
        account_type = _normalize_account_type(
            detected.get("config_type_name")
            or payload.get("account_type_name")
            or ctx.account_type_name
            or USED_FUEL_ACCOUNT_TYPE
        )
        profile = _motor_quote_profile(account_type) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        decision = _json_obj(normalized.get("license_type_decision"))
        license_type = _normalize_license_type_value(
            normalized.get("license_type")
            or normalized.get("licenseType")
            or decision.get("license_type")
            or decision.get("licenseType")
            or _profile_text(profile, "license_type")
        )
        if not license_type:
            license_type = "52" if "新能源" in account_type else "02"
        return {
            "plate_no": _clean_vehicle_cert_value("plate_no", normalized.get("plate_no")),
            "engine_no": _clean_vehicle_cert_value("engine_no", normalized.get("engine_no")),
            "vin": _clean_vehicle_cert_value("vin", normalized.get("vin")),
            "last_policy_no": _clean_vehicle_cert_value(
                "policy_no",
                _first_text(
                    normalized.get("last_policy_no"),
                    normalized.get("lastPolicyNo"),
                    normalized.get("policy_no"),
                    normalized.get("policyNo"),
                ),
            ),
            "license_type": license_type,
            "license_color_code": _license_color_for_type(license_type),
        }

    def _renewal_candidate_summary(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        item = _json_obj(row)
        return {
            "policy_no": _to_str(item.get("policyNo")).strip(),
            "policy_no_encode": _to_str(item.get("policyNoEncode")).strip(),
            "relation_policy_no": _to_str(item.get("relationPolicyNo")).strip(),
            "relation_policy_no_encode": _to_str(item.get("relationPolicyNoEncode")).strip(),
            "risk_code": _to_str(item.get("riskCode")).strip(),
            "license_no": _to_str(item.get("licenseNo")).strip(),
            "license_type": _normalize_license_type_value(item.get("licenseType")),
            "engine_no": _clean_vehicle_cert_value("engine_no", item.get("engineNo")),
            "vin": _clean_vehicle_cert_value("vin", item.get("frameNo") or item.get("vinNo")),
            "insured_name": _to_str(item.get("insuredName")).strip(),
            "end_date": _to_str(item.get("endDate")).strip(),
            "renewal_or_copy_flag": _to_str(item.get("renewalOrCopyFlag")).strip(),
            "car_kind_code": _to_str(item.get("carKindCode")).strip(),
            "no_dam_years_bi": _to_str(item.get("noDamYearsBI")).strip(),
            "no_dam_years_ci": _to_str(item.get("noDamYearsCI")).strip(),
            "last_damaged_bi": _to_str(item.get("lastDamagedBI")).strip(),
            "last_damaged_ci": _to_str(item.get("lastDamagedCI")).strip(),
            "raw": item,
        }

    def _renewal_lookup_param_attempts(
        self,
        *,
        plate_no: str,
        engine_no: str,
        vin: str,
        last_policy_no: str,
        license_type: str,
        is_owner: bool,
    ) -> List[Dict[str, Any]]:
        base = {
            "lastPolicyNo": "",
            "engineNo4Renew1": "",
            "frameNo4Renew1": "",
            "licenseNo4Renew": plate_no,
            "licenseType4Renew": license_type,
            "engineNo4Renew2": "",
            "frameNo4Renew2": "",
            "frameNo4Renew3": "",
            "rows": 10,
            "page": 1,
            "sort": "endDate",
            "order": "desc",
            "isOwner": "true" if is_owner else "false",
            "khyxRenewByLicenseNo": "0",
            "taskId": "",
        }
        attempts: List[Dict[str, Any]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()

        def add(strategy: str, *, license_type_override: str = "", **updates: Any) -> None:
            params = {
                **base,
                **({"licenseType4Renew": license_type_override} if license_type_override else {}),
                **{key: _to_str(value).strip() for key, value in updates.items()},
            }
            if not any(
                _to_str(params.get(key)).strip()
                for key in ("lastPolicyNo", "engineNo4Renew1", "frameNo4Renew1", "engineNo4Renew2", "frameNo4Renew2", "frameNo4Renew3")
            ):
                return
            signature = tuple(sorted((key, _to_str(value).strip()) for key, value in params.items()))
            if signature in seen:
                return
            seen.add(signature)
            attempts.append({"strategy": strategy, "params": params})

        if last_policy_no:
            policy_license_types = [license_type]
            for fallback_type in ("02", "52"):
                if fallback_type not in policy_license_types:
                    policy_license_types.append(fallback_type)
            for item_license_type in policy_license_types:
                strategy = "last_policy_no" if item_license_type == license_type else f"last_policy_no_license_{item_license_type}"
                add(strategy, license_type_override=item_license_type, lastPolicyNo=last_policy_no)
        lookup_license_types = [license_type]
        for fallback_type in ("02", "52"):
            if fallback_type not in lookup_license_types:
                lookup_license_types.append(fallback_type)
        if len(engine_no) >= 4:
            for item_license_type in lookup_license_types:
                strategy = "engine_last4" if item_license_type == license_type else f"engine_last4_license_{item_license_type}"
                add(strategy, license_type_override=item_license_type, engineNo4Renew2=engine_no[-4:])
        if len(vin) >= 6:
            for item_license_type in lookup_license_types:
                strategy = "vin_last6" if item_license_type == license_type else f"vin_last6_license_{item_license_type}"
                add(strategy, license_type_override=item_license_type, frameNo4Renew2=vin[-6:])
        return attempts

    def _query_renewal_sync(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        client: Optional[PiccProtocolClient] = None
        vehicle: Dict[str, str] = {}
        try:
            vehicle = self._renewal_lookup_vehicle(ctx, quote_payload)
            plate_no = _to_str(vehicle.get("plate_no")).strip()
            engine_no = re.sub(r"[^A-Z0-9]", "", _to_str(vehicle.get("engine_no")).upper())
            vin = re.sub(r"[^A-Z0-9]", "", _to_str(vehicle.get("vin")).upper())
            last_policy_no = _to_str(vehicle.get("last_policy_no")).strip()
            license_type = _normalize_license_type_value(vehicle.get("license_type")) or "02"
            if not plate_no:
                raise PiccRequestError("人保续保查询缺少号牌号码")
            if not engine_no and not vin and not last_policy_no:
                raise PiccRequestError("人保续保查询缺少上年保单号、发动机号或车架号")

            client = self._client(ctx)
            headers = {"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"}
            owner_check = client.request_json(
                "GET",
                RENEWAL_CHECK_OWNER_PATH,
                purpose="business",
                params={
                    "lastPolicyNo": "",
                    "licenseNo4Renew": plate_no,
                    "vinNo": vin,
                    "taskId": "",
                },
                headers=headers,
            )
            _ensure_platform_success(owner_check, action="续保车主校验")
            is_owner = _to_str(_json_obj(owner_check).get("data")).strip().lower() == "true"
            lookup_attempts = self._renewal_lookup_param_attempts(
                plate_no=plate_no,
                engine_no=engine_no,
                vin=vin,
                last_policy_no=last_policy_no,
                license_type=license_type,
                is_owner=is_owner,
            )
            if not lookup_attempts:
                raise PiccRequestError("人保续保查询缺少可用查询条件")

            candidates: List[Dict[str, Any]] = []
            renewal_response: Dict[str, Any] = {}
            lookup_params: Dict[str, Any] = {}
            attempt_records: List[Dict[str, Any]] = []
            candidate_map: Dict[str, Dict[str, Any]] = {}
            not_found_pattern = re.compile(r"(没有此车辆信息|不是可续保车辆|不可续保|无续保信息|未查询到续保|没有续保信息)")

            def candidate_key(row: Mapping[str, Any]) -> str:
                return (
                    _to_str(row.get("policy_no_encode")).strip()
                    or _to_str(row.get("policy_no")).strip()
                    or "|".join(
                        [
                            _to_str(row.get("risk_code")).strip(),
                            _renewal_compare_text(row.get("license_no")),
                            _renewal_compare_text(row.get("vin")),
                            _to_str(row.get("end_date")).strip(),
                        ]
                    )
                )

            def add_candidates(rows: List[Dict[str, Any]], strategy: str) -> None:
                for row in rows:
                    key = candidate_key(row)
                    if not key:
                        continue
                    existing = candidate_map.get(key)
                    if existing is None:
                        existing = dict(row)
                        existing["source_strategies"] = []
                        candidate_map[key] = existing
                    strategies = existing.get("source_strategies")
                    if not isinstance(strategies, list):
                        strategies = []
                        existing["source_strategies"] = strategies
                    if strategy not in strategies:
                        strategies.append(strategy)

            for attempt in lookup_attempts:
                strategy = _to_str(attempt.get("strategy")).strip() or "unknown"
                params = _json_obj(attempt.get("params"))
                response: Dict[str, Any] = {}
                try:
                    response = client.request_json(
                        "GET",
                        RENEWAL_QUOTE_SEARCH_PATH,
                        purpose="business",
                        params=params,
                        headers=headers,
                    )
                    _ensure_platform_success(response, action="续保查询")
                except PiccBusinessRequestError as exc:
                    message = _platform_message(getattr(exc, "platform_response", None), str(exc) or "续保查询失败")
                    compact_message = re.sub(r"\s+", "", message)
                    treat_as_not_found = bool(not_found_pattern.search(compact_message)) or (
                        strategy == "last_policy_no" and "查询参数错误" in compact_message
                    )
                    attempt_records.append(
                        {
                            "strategy": strategy,
                            "params": params,
                            "status": "not_found" if treat_as_not_found else "failed",
                            "message": message,
                            "platform_response": _platform_debug_payload(getattr(exc, "platform_response", None)),
                        }
                    )
                    if not treat_as_not_found:
                        raise
                    renewal_response = _json_obj(getattr(exc, "platform_response", None))
                    lookup_params = params
                    continue

                response_data = _json_obj(_json_obj(response).get("data"))
                raw_rows = response_data.get("list")
                attempt_candidates = [
                    self._renewal_candidate_summary(row)
                    for row in raw_rows
                    if isinstance(row, Mapping)
                ] if isinstance(raw_rows, list) else []
                attempt_candidates = [
                    row
                    for row in attempt_candidates
                    if row.get("policy_no") or row.get("policy_no_encode")
                ]
                message = _platform_message(response, "")
                attempt_records.append(
                    {
                        "strategy": strategy,
                        "params": params,
                        "status": "found" if attempt_candidates else "not_found",
                        "message": message,
                        "candidate_count": len(attempt_candidates),
                    }
                )
                renewal_response = response
                lookup_params = params
                if attempt_candidates:
                    add_candidates(attempt_candidates, strategy)

            candidates = sorted(
                candidate_map.values(),
                key=lambda row: _renewal_candidate_sort_value(row, vehicle),
                reverse=True,
            )
            selected = _pick_renewal_policy_candidate(candidates, vehicle)
            selected_score = _renewal_candidate_score(selected, vehicle) if selected else 0
            selected_reason = _renewal_candidate_selection_reason(selected, vehicle) if selected else ""

            lookup_payload = {
                "vehicle": vehicle,
                "check_is_owner": is_owner,
                "params": lookup_params,
                "attempts": attempt_records,
                "candidates": candidates,
                "selected": selected,
                "selected_score": selected_score,
                "selected_reason": selected_reason,
            }
            if not candidates:
                message = _platform_message(renewal_response, "")
                if not re.search(
                    r"(没有此车辆信息|不是可续保车辆|不可续保|无续保信息|未查询到续保)",
                    re.sub(r"\s+", "", message),
                ):
                    message = "没有此车辆信息或不是可续保车辆"
                return PlatformRuntimeResult(
                    status="success",
                    message=message,
                    data=success_data(
                        client,
                        extra={
                            "business_status": "renewal_not_found",
                            "renewal_found": False,
                            "renewal_lookup": lookup_payload,
                            "platform_response": _platform_debug_payload(renewal_response),
                        },
                    ),
                )
            if selected and selected_score < 0:
                lookup_payload["found"] = False
                lookup_payload["reject_reason"] = "续保候选与当前车辆信息不一致"
                return PlatformRuntimeResult(
                    status="success",
                    message="人保续保查询到候选保单，但与当前车辆信息不一致，已按未找到续保处理",
                    data=success_data(
                        client,
                        extra={
                            "business_status": "renewal_not_found",
                            "renewal_found": False,
                            "renewal_lookup": lookup_payload,
                            "platform_response": _platform_debug_payload(renewal_response),
                        },
                    ),
                )
            return PlatformRuntimeResult(
                status="success",
                message="已查询到可续保保单",
                data=success_data(
                    client,
                    extra={
                        "business_status": "renewal_found",
                        "renewal_found": True,
                        "renewal_lookup": lookup_payload,
                        "platform_response": _platform_debug_payload(renewal_response),
                    },
                ),
            )
        except PiccSessionExpiredError as exc:
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 登录已过期，请重新登录",
                data={
                    "business_status": "16",
                    "error_code": exc.__class__.__name__,
                    "renewal_lookup": {"vehicle": vehicle},
                },
            )
        except PiccTransientGatewayError as exc:
            payload: Dict[str, Any] = {
                "error_code": exc.__class__.__name__,
                "transient": True,
                "renewal_lookup": {"vehicle": vehicle},
            }
            if client is not None:
                payload = success_data(client, extra=payload)
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data=payload,
            )
        except PiccRequestError as exc:
            payload = {
                "error_code": exc.__class__.__name__,
                "error_stage": getattr(exc, "action", "") or "renewal_lookup",
                "renewal_lookup": {"vehicle": vehicle},
            }
            if isinstance(exc, PiccBusinessRequestError):
                payload["platform_response"] = _platform_debug_payload(getattr(exc, "platform_response", None))
                payload["platform_dialog"] = _platform_business_error_dialog(getattr(exc, "platform_response", None))
            if client is not None:
                payload = success_data(client, extra=payload)
            return PlatformRuntimeResult(status="failed", message=str(exc) or "人保续保查询失败", data=payload)
        except Exception as exc:
            payload = {
                "error_code": exc.__class__.__name__,
                "error_stage": "renewal_lookup",
                "renewal_lookup": {"vehicle": vehicle},
            }
            if client is not None:
                payload = success_data(client, extra=payload)
            return PlatformRuntimeResult(status="failed", message=str(exc) or "人保续保查询失败", data=payload)

    def _fetch_renewal_policy_prefill(
        self,
        client: PiccProtocolClient,
        candidate: Mapping[str, Any],
    ) -> Dict[str, Any]:
        row = _json_obj(candidate.get("raw")) or _json_obj(candidate)
        policy_no = _to_str(row.get("policyNo") or candidate.get("policy_no")).strip()
        policy_no_encode = _to_str(row.get("policyNoEncode") or candidate.get("policy_no_encode")).strip()
        if not policy_no or not policy_no_encode:
            raise PiccRequestError("人保续保一键回填缺少保单号或加密保单号")
        encrypted_policy_no = _picc_encrypt_renewal_policy_no(policy_no)
        data = client.request_json(
            "GET",
            RENEWAL_QUOTE_POLICY_PATH,
            purpose="business",
            params={
                "policyNo": encrypted_policy_no,
                "policyNoEncode": policy_no_encode,
            },
            headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
        )
        _ensure_platform_success(data, action="续保一键回填")
        payload = _json_obj(_json_obj(data).get("data"))
        if not payload:
            raise PiccRequestError("人保续保一键回填未返回可用资料")
        return {
            "request": {
                "policyNo": encrypted_policy_no,
                "policyNoEncode": policy_no_encode,
                "plainPolicyNo": policy_no,
            },
            "response": payload,
            "platform_response": _platform_debug_payload(data),
        }

    def _renewal_prefill_vehicle_data(self, prefill: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[str, Any]:
        data = _json_obj(prefill.get("response") or prefill)
        car = _clean_vehicle_cert_fields(_json_obj(data.get("renewItemCarVo")))
        main = _json_obj(data.get("renewMainVo"))
        selected_license_type = _normalize_license_type_value(
            car.get("licenseType")
            or candidate.get("license_type")
            or _json_obj(candidate.get("raw")).get("licenseType")
        )
        vin = _clean_vehicle_cert_value("vin", _first_text(car.get("vinNo"), car.get("frameNo"), candidate.get("vin")))
        engine_no = _clean_vehicle_cert_value("engine_no", _first_text(car.get("engineNo"), candidate.get("engine_no")))
        plate_no = _clean_vehicle_cert_value("plate_no", _first_text(car.get("licenseNo"), candidate.get("license_no")))
        start_date_bi = _renewal_next_start_date(main.get("endDate") or candidate.get("end_date"))
        start_date_ci = _renewal_next_start_date(main.get("endDateCI") or candidate.get("end_date"))
        account_type_name = NEW_ENERGY_USED_ACCOUNT_TYPE if selected_license_type == "52" else USED_FUEL_ACCOUNT_TYPE
        out = {
            "account_type_name": account_type_name,
            "plate_no": plate_no,
            "engine_no": engine_no,
            "vin": vin,
            "owner_name": _first_text(car.get("carOwner"), candidate.get("insured_name")),
            "vehicle_model": _first_text(car.get("brandName"), car.get("modelName")),
            "vehicle_brand_name": _first_text(car.get("brandName"), car.get("modelName")),
            "first_register_date": _date_text(car.get("enrollDate")),
            "commercial_start_date": start_date_bi,
            "compulsory_start_date": start_date_ci,
            "approved_passenger_count": _first_text(car.get("seatCount"), "5"),
            "license_type": selected_license_type,
            "license_color_code": _license_color_for_type(selected_license_type),
            "license_type_decision": _json_obj(
                {
                    "license_type": selected_license_type,
                    "license_color_code": _license_color_for_type(selected_license_type),
                    "source": "renewal_policy_prefill",
                    "reason": "人保续保一键回填返回号牌种类",
                }
            ),
            "renewal_policy_prefill": {
                "policy_no": _first_text(main.get("policyNo"), candidate.get("policy_no")),
                "policy_ci_no": _first_text(main.get("policyCINo"), candidate.get("relation_policy_no")),
                "proposal_no_bi": _first_text(main.get("proposalNoBI"), _json_obj(data.get("renewMainSub")).get("proposalNoBI")),
                "proposal_no_ci": _first_text(main.get("proposalNoCI"), _json_obj(data.get("renewMainSub")).get("proposalNoCI")),
                "start_date_bi": start_date_bi,
                "start_date_ci": start_date_ci,
            },
        }
        quote_overrides = self._renewal_product_defaults_from_prefill(data)
        if quote_overrides:
            out["renewal_quote_field_defaults"] = quote_overrides
        vehicle_form = {
            "licenseNo": plate_no,
            "licenseType": selected_license_type,
            "licenseColorCode": _license_color_for_type(selected_license_type),
            "engineNo": engine_no,
            "vin": vin,
            "carKindCode": _first_text(car.get("carKindCode"), candidate.get("car_kind_code"), "A01"),
            "useNatureCode": _first_text(car.get("useNatureCode"), "211"),
            "enrollDate": _date_text(car.get("enrollDate")),
            "startDateBI": start_date_bi,
            "startDateCI": start_date_ci,
            "modelName": _first_text(car.get("brandName"), car.get("modelName")),
            "rawModelName": _first_text(car.get("brandName"), car.get("modelName")),
            "seatCount": _first_text(car.get("seatCount"), "5"),
            "purchasePrice": _clean_money_text_or_empty(car.get("purchasePrice")),
            "actualValue": _money_text_or_empty(car.get("actualValue")),
            "modelCode": _to_str(car.get("modelCode")).strip(),
            "platformModelCode": _to_str(car.get("modelCode")).strip(),
            "selectedVehicleId": _to_str(car.get("modelCode")).strip(),
            "selectedModelName": _first_text(car.get("brandName"), car.get("modelName")),
        }
        out["renewal_request_body_seed"] = {
            "vehicleForm": _clean_vehicle_cert_fields(vehicle_form),
            "ownerForm": {
                "ownerName": _first_text(car.get("carOwner"), candidate.get("insured_name")),
            },
            "preflight": {
                "renewalPolicyPrefill": {
                    "request": _json_obj(prefill.get("request")),
                    "policyNo": _first_text(main.get("policyNo"), candidate.get("policy_no")),
                    "policyCINo": _first_text(main.get("policyCINo"), candidate.get("relation_policy_no")),
                }
            },
        }
        return _clean_vehicle_cert_fields({key: value for key, value in out.items() if value not in (None, "")})

    def _renewal_product_defaults_from_prefill(self, policy_data: Mapping[str, Any]) -> Dict[str, Any]:
        rows = policy_data.get("renewItemKindVoList") if isinstance(policy_data.get("renewItemKindVoList"), list) else []
        defaults: Dict[str, Any] = {}
        for row_any in rows:
            row = _json_obj(row_any)
            kind_code = _to_str(row.get("kindCode")).strip()
            amount = _clean_money_text_or_empty(row.get("amount"))
            unit_amount = _clean_money_text_or_empty(row.get("unitAmount"))
            if kind_code == "051051" and _is_positive_amount(amount):
                defaults.setdefault(PRODUCT_THIRD_PARTY, _wan_or_amount_to_wan_text(amount, "300"))
            elif kind_code == "051052":
                value = unit_amount or amount
                if _is_positive_amount(value):
                    defaults.setdefault(PRODUCT_DRIVER, value)
            elif kind_code == "051053":
                value = unit_amount or amount
                if _is_positive_amount(value):
                    defaults.setdefault(PRODUCT_PASSENGER, value)
            elif kind_code == "051063" and _is_positive_amount(amount):
                defaults.setdefault(PRODUCT_MEDICAL_THIRD, amount)
                if _to_str(row.get("sharedAmountFlag")).strip() == "1":
                    defaults.setdefault(PRODUCT_SHARED_LIMIT, True)
            elif kind_code == "051064":
                quantity = _safe_int_local(row.get("quantity"), 0)
                if quantity > 0:
                    defaults.setdefault(PRODUCT_ROAD_RESCUE, str(quantity))
            elif kind_code == "051085" and _is_positive_amount(amount):
                defaults.setdefault(PRODUCT_EXTERNAL_GRID, amount)
            elif kind_code == "051074" and _is_positive_amount(amount):
                defaults.setdefault(PRODUCT_COMPULSORY, _wan_or_amount_to_wan_text(amount, "20"))
        return defaults

    def _apply_renewal_prefill_to_quote_body(
        self,
        body: Mapping[str, Any],
        renewal_data: Mapping[str, Any],
        prefill: Mapping[str, Any],
        selected: Mapping[str, Any],
    ) -> Dict[str, Any]:
        out = _clean_used_fuel_request_body(body)
        form = _clean_vehicle_cert_fields(_json_obj(out.get("quoteForm")))
        vehicle = _clean_vehicle_cert_fields(_json_obj(out.get("vehicleForm")))
        if not form:
            out["quoteForm"] = form
            return out

        policy = _json_obj(renewal_data.get("renewal_policy_prefill"))
        response = _json_obj(prefill.get("response"))
        car = _clean_vehicle_cert_fields(_json_obj(response.get("renewItemCarVo")))
        main = _json_obj(response.get("renewMainVo"))
        main_sub = _json_obj(response.get("renewMainSub"))

        policy_no_bi = _first_text(
            policy.get("policy_no"),
            main.get("policyNo"),
            main_sub.get("policyNoBI"),
            selected.get("relation_policy_no") if _to_str(selected.get("risk_code")).strip().upper() == "DZA" else selected.get("policy_no"),
        )
        policy_no_ci = _first_text(
            policy.get("policy_ci_no"),
            main.get("policyCINo"),
            main_sub.get("policyNoCI"),
            selected.get("policy_no") if _to_str(selected.get("risk_code")).strip().upper() == "DZA" else selected.get("relation_policy_no"),
        )
        start_date_bi = _first_text(
            _date_text(policy.get("start_date_bi")),
            _date_text(renewal_data.get("commercial_start_date")),
            form.get("prpCmain.startDate"),
        )
        start_date_ci = _first_text(
            _date_text(policy.get("start_date_ci")),
            _date_text(renewal_data.get("compulsory_start_date")),
            form.get("prpCmain.startDateCI"),
        )

        form.update(
            {
                "renewed": "1",
                "lastPolicyNo": policy_no_ci or policy_no_bi,
                "prpCitemCar.lastBIPolicyNo": policy_no_bi,
                "prpCitemCar.lastCIPolicyNo": policy_no_ci,
                "prpCitemCar.Nodamageyears": _first_text(main.get("noDamYearsBI"), selected.get("no_dam_years_bi"), "0"),
                "prpCitemCarExt.noDamYearsBI": _first_text(main.get("noDamYearsBI"), selected.get("no_dam_years_bi"), "0"),
                "prpCitemCarExt.lastDamagedBI": _first_text(main.get("lastDamagedBI"), selected.get("last_damaged_bi"), "0"),
                "prpCitemCarExt.lastDamagedCI": _first_text(main.get("lastDamagedCI"), selected.get("last_damaged_ci"), "0"),
                "prpCitemCarExt.thisDamagedBI": _first_text(main.get("thisDamagedBI"), "0"),
                "prpCcarShipTax.leviedDate": _first_text(start_date_bi, start_date_ci, form.get("prpCcarShipTax.leviedDate")),
                "prpCcarShipTax.payStartDate": _first_text(
                    form.get("prpCcarShipTax.payStartDate"),
                    _year_start_date(start_date_ci or start_date_bi),
                ),
                "prpCcarShipTax.payEndDate": _first_text(
                    form.get("prpCcarShipTax.payEndDate"),
                    _year_end_date(start_date_ci or start_date_bi),
                ),
                "prpCcarShipTax.taxAbateAmount": _first_text(main.get("taxabateamount"), car.get("taxabateamount"), "0"),
                "prpCcarShipTax.taxAbateProportion": _first_text(main.get("taxabateproportion"), car.get("taxabateproportion"), "0"),
            }
        )
        if _to_str(car.get("lastUserclassificationCode")).strip():
            form["prpCitemCar.lastUserclassificationCode"] = _to_str(car.get("lastUserclassificationCode")).strip()
        if _to_str(car.get("lastCarChecker")).strip():
            form["prpCitemCar.lastCarChecker"] = _to_str(car.get("lastCarChecker")).strip()
        if _to_str(car.get("lastEndDateBI")).strip():
            form["prpCitemCar.lastEndDateBI"] = _to_str(car.get("lastEndDateBI")).strip()
        if _to_str(car.get("lastEndDateCI")).strip():
            form["prpCitemCar.lastEndDateCI"] = _to_str(car.get("lastEndDateCI")).strip()
        if _to_str(car.get("taxRegistryNumber")).strip():
            form["prpCcarShipTax.taxregistrynumber"] = _to_str(car.get("taxRegistryNumber")).strip()
        if _to_str(car.get("taxcomcode")).strip():
            form["prpCcarShipTax.taxcomcode"] = _to_str(car.get("taxcomcode")).strip()
        if _to_str(car.get("taxcomname")).strip():
            form["prpCcarShipTax.taxcomname"] = _to_str(car.get("taxcomname")).strip()

        vehicle["lastBIPolicyNo"] = policy_no_bi
        vehicle["lastCIPolicyNo"] = policy_no_ci
        vehicle["lastDamagedBI"] = form.get("prpCitemCarExt.lastDamagedBI")
        vehicle["lastDamagedCI"] = form.get("prpCitemCarExt.lastDamagedCI")
        vehicle["noDamYearsBI"] = form.get("prpCitemCarExt.noDamYearsBI")
        out["quoteForm"] = _clean_vehicle_cert_fields(form)
        out["vehicleForm"] = _clean_vehicle_cert_fields(vehicle)
        return _clean_used_fuel_request_body(out)

    def _prepare_renewal_used_fuel_quote(
        self,
        client: PiccProtocolClient,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        *,
        account_type_name: str = USED_FUEL_ACCOUNT_TYPE,
    ) -> Dict[str, Any]:
        payload = _json_obj(quote_payload)
        normalized_data = _json_obj(payload.get("normalized_data"))
        renewal_lookup = _json_obj(normalized_data.get("renewal_lookup") or payload.get("renewal_lookup"))
        selected = _json_obj(renewal_lookup.get("selected"))
        if not selected:
            candidates = renewal_lookup.get("candidates") if isinstance(renewal_lookup.get("candidates"), list) else []
            selected = _pick_renewal_policy_candidate(
                [dict(item) for item in candidates if isinstance(item, Mapping)],
                normalized_data,
            )
        if not selected:
            # Older cases stored only the selected policy summary. Keep those
            # sessions requotable after an adjustment such as "司乘改3万".
            legacy_policy_no = _to_str(renewal_lookup.get("selected_policy_no")).strip()
            legacy_policy_no_encode = _to_str(renewal_lookup.get("selected_policy_no_encode")).strip()
            if legacy_policy_no and legacy_policy_no_encode:
                selected = {
                    "policy_no": legacy_policy_no,
                    "policy_no_encode": legacy_policy_no_encode,
                    "risk_code": _to_str(renewal_lookup.get("selected_risk_code")).strip(),
                    "end_date": _to_str(renewal_lookup.get("selected_end_date")).strip(),
                    "license_type": _normalize_license_type_value(renewal_lookup.get("selected_license_type")),
                }
        if not selected:
            raise PiccRequestError("人保续保报价缺少可用续保保单，请重新发起续保查询")
        prefill = self._fetch_renewal_policy_prefill(client, selected)
        renewal_data = self._renewal_prefill_vehicle_data(prefill, selected)
        # OCR / user-corrected case data stays above renewal prefill; renewal
        # only supplies missing vehicle fields, except license type because it
        # is platform-confirmed by quotePolicy.do and drives the account type.
        merged_normalized = _deep_merge(renewal_data, normalized_data)
        renewal_license_type = _normalize_license_type_value(renewal_data.get("license_type"))
        if renewal_license_type:
            merged_normalized["license_type"] = renewal_license_type
            merged_normalized["license_color_code"] = _license_color_for_type(renewal_license_type)
            merged_normalized["account_type_name"] = (
                NEW_ENERGY_USED_ACCOUNT_TYPE if renewal_license_type == "52" else USED_FUEL_ACCOUNT_TYPE
            )
            merged_normalized["license_type_decision"] = _json_obj(renewal_data.get("license_type_decision"))
        renewal_defaults = _json_obj(renewal_data.get("renewal_quote_field_defaults"))
        user_overrides = _json_obj(normalized_data.get("quote_field_overrides"))
        configured_defaults = _json_obj(payload.get("default_config_json"))
        default_config = _picc_business_defaults(configured_defaults)
        merged_defaults = dict(default_config)
        accepted_renewal_defaults: Dict[str, Any] = {}
        ignored_configured_renewal_defaults: Dict[str, Any] = {}
        ignored_invalid_renewal_defaults: Dict[str, Any] = {}
        ignored_user_override_renewal_defaults: Dict[str, Any] = {}
        ignored_unconfigured_optional_renewal_defaults: Dict[str, Any] = {}
        for key, value in renewal_defaults.items():
            if _has_configured_product_default(user_overrides, key):
                ignored_user_override_renewal_defaults[key] = value
                continue
            if _has_configured_product_default(configured_defaults, key):
                ignored_configured_renewal_defaults[key] = value
                continue
            if (
                key == PRODUCT_SHARED_LIMIT
                or _is_positive_amount(value)
            ):
                merged_defaults[key] = value
                accepted_renewal_defaults[key] = value
            else:
                ignored_invalid_renewal_defaults[key] = value
        merged_defaults.update(user_overrides)
        renewal_payload = dict(payload)
        renewal_payload["normalized_data"] = merged_normalized
        renewal_payload["default_config_json"] = merged_defaults
        effective_account_type_name = _normalize_account_type(merged_normalized.get("account_type_name") or account_type_name)
        body = self._prepare_used_fuel_quote(client, ctx, renewal_payload, account_type_name=effective_account_type_name)
        body = self._apply_renewal_prefill_to_quote_body(body, renewal_data, prefill, selected)
        preflight = dict(_json_obj(body.get("preflight")))
        preflight["renewalPolicyPrefill"] = {
            **_json_obj(prefill.get("request")),
            "selected": selected,
            "vehicle": _json_obj(renewal_data.get("renewal_request_body_seed")).get("vehicleForm"),
            "policy": _json_obj(renewal_data.get("renewal_policy_prefill")),
        }
        if renewal_defaults:
            preflight["renewalQuoteFieldDefaults"] = renewal_defaults
            preflight["renewalQuoteFieldPriority"] = "会话明确调参值 > 默认参数配置 > 有效续保接口返回值 > profile内置默认值"
            preflight["renewalMergeTrace"] = {
                "effectiveAccountTypeName": effective_account_type_name,
                "acceptedRenewalDefaults": accepted_renewal_defaults,
                "ignoredRenewalDefaults": ignored_invalid_renewal_defaults,
                "ignoredConfiguredRenewalDefaults": ignored_configured_renewal_defaults,
                "ignoredUserOverrideRenewalDefaults": ignored_user_override_renewal_defaults,
                "ignoredUnconfiguredOptionalRenewalDefaults": ignored_unconfigured_optional_renewal_defaults,
                "userOverrideKeys": list(user_overrides.keys()),
            }
        body["preflight"] = preflight
        body["renewalPolicyPrefill"] = _json_obj(renewal_data.get("renewal_policy_prefill"))
        return _clean_used_fuel_request_body(body)

    def _query_joint_sales_plan_sync(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        payload = _json_obj(quote_payload)
        premium = _money(payload.get("premium"), "0")
        premium_text = _clean_money_text(premium)
        if premium <= 0:
            plan = {
                "enabled": False,
                "attempted": False,
                "success": True,
                "premium": "0",
                "amount": "0",
                "reason": "途家安顺保费为0，按规则不查询保额",
                "risk_code": PICC_TUJIA_ANSHUN_RISK_CODE,
                "brand_id": PICC_TUJIA_ANSHUN_BRAND_ID,
                "service_group_type_code": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
            }
            return PlatformRuntimeResult(
                status="success",
                message="途家安顺保费为0，保额已按规则置为0",
                data={"joint_sales_plan": plan, "premium": "0", "amount": "0", "query_skipped": True},
            )

        client: Optional[PiccProtocolClient] = None
        try:
            client = self._client(ctx)
            probe = client.request_json(
                "GET",
                KEEPALIVE_PATH,
                purpose="business",
                params=KEEPALIVE_PARAMS,
                headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
            )
            if isinstance(probe, Mapping) and int(probe.get("status", -1)) != 0:
                status_code = int(probe.get("status", -1))
                runtime_status = "expired" if status_code == 16 else "failed"
                return PlatformRuntimeResult(
                    status=runtime_status,
                    message=str(probe.get("statusText") or probe),
                    data=success_data(
                        client,
                        extra={
                            "business_status": probe.get("status"),
                            "platform_status_text": probe.get("statusText") or "",
                            "premium": premium_text,
                        },
                    ),
                )

            request_body = _json_obj(payload.get("request_body"))
            defaults = _picc_business_defaults(payload.get("default_config_json"))
            request_defaults = _json_obj(request_body.get("defaultFields"))
            if request_defaults:
                defaults = _deep_merge(defaults, request_defaults)
            defaults[PRODUCT_TUJIA_ANSHUN_PREMIUM] = premium_text
            plan = self._query_tujia_anshun_plan_best_effort(
                client,
                defaults,
                _json_obj(request_body.get("quoteForm")),
            )
            if not plan.get("success"):
                return PlatformRuntimeResult(
                    status="failed",
                    message=_to_str(plan.get("message")).strip() or f"未查询到保费为{premium_text}的途家安顺方案",
                    data=success_data(
                        client,
                        extra={
                            "joint_sales_plan": plan,
                            "premium": premium_text,
                            "amount": _clean_money_text(plan.get("amount"), "0"),
                        },
                    ),
                )
            amount = _clean_money_text(plan.get("amount"), "0")
            return PlatformRuntimeResult(
                status="success",
                message="途家安顺保额查询成功",
                data=success_data(
                    client,
                    extra={
                        "joint_sales_plan": plan,
                        "premium": _clean_money_text(plan.get("premium"), premium_text),
                        "amount": amount,
                    },
                ),
            )
        except PiccSessionExpiredError as exc:
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 当前账号没有可用会话，请先登录",
                data={"business_status": "16", "error_code": exc.__class__.__name__, "premium": premium_text},
            )
        except PiccTransientGatewayError as exc:
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data={"error_code": exc.__class__.__name__, "transient": True, "premium": premium_text},
            )
        except Exception as exc:
            extra: Dict[str, Any] = {"error_code": exc.__class__.__name__, "premium": premium_text}
            if client is not None:
                extra = success_data(client, extra=extra)
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data=extra,
            )

    def _query_repair_codes_sync(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        payload = _json_obj(quote_payload)
        query_text = _to_str(payload.get("query") or payload.get("keyword")).strip()
        client: Optional[PiccProtocolClient] = None
        try:
            client = self._client(ctx)
            probe = client.request_json(
                "GET",
                KEEPALIVE_PATH,
                purpose="business",
                params=KEEPALIVE_PARAMS,
                headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
            )
            if isinstance(probe, Mapping) and int(probe.get("status", -1)) != 0:
                status_code = int(probe.get("status", -1))
                runtime_status = "expired" if status_code == 16 else "failed"
                return PlatformRuntimeResult(
                    status=runtime_status,
                    message=str(probe.get("statusText") or probe),
                    data=success_data(
                        client,
                        extra={
                            "business_status": probe.get("status"),
                            "platform_status_text": probe.get("statusText") or "",
                            "query": query_text,
                        },
                    ),
                )
            data = client.request_json(
                "GET",
                MONOPOLY_QUERY_PATH,
                purpose="business",
                params={"rows": _to_str(payload.get("rows") or "1000")},
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            _ensure_platform_success(data, action="送修码查询")
            response_data = _json_obj(_json_obj(data).get("data"))
            raw_rows = response_data.get("list") or response_data.get("rows") or response_data.get("items")
            if raw_rows is None and isinstance(_json_obj(data).get("list"), list):
                raw_rows = _json_obj(data).get("list")
            rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)] if isinstance(raw_rows, list) else []
            return PlatformRuntimeResult(
                status="success",
                message="送修码查询成功",
                data=success_data(
                    client,
                    extra={
                        "query": query_text,
                        "rows": rows,
                        "total": len(rows),
                    },
                ),
            )
        except PiccSessionExpiredError as exc:
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 当前账号没有可用会话，请先登录",
                data={"business_status": "16", "error_code": exc.__class__.__name__, "query": query_text},
            )
        except PiccTransientGatewayError as exc:
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data={"error_code": exc.__class__.__name__, "transient": True, "query": query_text},
            )
        except Exception as exc:
            extra: Dict[str, Any] = {"error_code": exc.__class__.__name__, "query": query_text}
            if client is not None:
                extra = success_data(client, extra=extra)
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data=extra,
            )

    def _rebuild_quote_for_platform_vehicle_code(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        codes: List[str],
    ) -> tuple[Dict[str, Any], bool]:
        body = _clean_used_fuel_request_body(request_body)
        if not codes:
            return body, False
        defaults = _json_obj(body.get("defaultFields"))
        vehicle = _clean_vehicle_cert_fields(_json_obj(body.get("vehicleForm")))
        owner = _clean_vehicle_cert_fields(_json_obj(body.get("ownerForm")))
        if not defaults or not vehicle or not owner:
            return body, False

        account_type_name = _normalize_account_type(body.get("accountTypeName") or USED_FUEL_ACCOUNT_TYPE)
        profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        vehicle_search_preflight = _json_obj(_json_obj(body.get("preflight")).get("vehicleSearch"))
        explicit_loss_amount = (
            Decimal("0")
            if _product_excluded(defaults, PRODUCT_LOSS)
            else _money(vehicle_search_preflight.get("requestedLossAmount"))
        )
        rows = _vehicle_rows(self._query_vehicle_candidates(client, vehicle, profile=profile, defaults=defaults))
        matching_rows: List[Dict[str, Any]] = []
        selected_code = ""
        wanted = {code.upper() for code in codes if code}
        for row in rows:
            row_codes = _vehicle_row_codes(row)
            matched = next((code for code in row_codes if code in wanted), "")
            if matched:
                matched_row = dict(row)
                matched_row["_platformReturnedCode"] = matched
                matching_rows.append(matched_row)
        selected = _pick_best_vehicle_candidate(matching_rows, vehicle, explicit_loss_amount=explicit_loss_amount)
        if selected:
            selected_code = _to_str(selected.get("_platformReturnedCode")).strip()
        if not selected:
            return body, False

        precise_result = self._query_precise_vehicle(client, vehicle, defaults, selected, profile=profile)
        precise_rows = _vehicle_rows(precise_result)
        precise_vehicle = (
            _pick_best_vehicle_candidate(precise_rows, vehicle, explicit_loss_amount=explicit_loss_amount)
            if precise_rows
            else {}
        )
        actual_value_result = self._query_actual_value(client, vehicle, defaults, selected, precise_vehicle, profile=profile)
        platform_purchase_price = _vehicle_platform_purchase_price(selected, precise_vehicle)
        actual_value = _actual_value_from_response(actual_value_result, platform_purchase_price or selected.get("actualValue"))
        if _profile_text(profile, "new_car_flag") and platform_purchase_price:
            actual_value = platform_purchase_price

        selected_price = _money(platform_purchase_price)
        vehicle_model_code = _first_text(selected_code, selected.get("vehicleModelCode"), precise_vehicle.get("platModelCode"))
        vehicle.update(
            {
                "purchasePrice": str(int(selected_price)) if selected_price == selected_price.to_integral() else str(selected_price),
                "actualValue": _money_text(actual_value),
                "modelCode": _first_text(selected.get("vehicleId"), selected.get("modelCode"), precise_vehicle.get("vehicleId")),
                "platformModelCode": _first_text(selected.get("vehicleId"), selected.get("modelCode"), precise_vehicle.get("vehicleId")),
                "platModelCode": vehicle_model_code,
                "platformVehicleModelCode": vehicle_model_code,
                "selectedModelName": _first_text(selected.get("vehicleName"), precise_vehicle.get("vehicleName"), vehicle.get("modelName")),
                "selectedVehicleAlias": _first_text(selected.get("vehicleAlias"), precise_vehicle.get("vehicleAlias")),
                "selectedVehicleId": _first_text(selected.get("vehicleId"), precise_vehicle.get("vehicleId")),
                "vehicleFgwCode": _model_search_code(_vehicle_platform_fgw_code(vehicle, selected, precise_vehicle)),
                "platformBrandId": _vehicle_platform_brand_id(selected, precise_vehicle),
                "platformBrandIDNew": _vehicle_platform_brand_id_new(
                    selected,
                    precise_vehicle,
                    _vehicle_platform_brand_id(selected, precise_vehicle),
                ),
            }
        )
        products = self._used_fuel_products(defaults, profile=profile, actual_value=actual_value, seat_count=vehicle.get("seatCount"))
        quote_form = self._build_used_fuel_quote_form(defaults, vehicle, owner, selected, precise_vehicle, products, profile=profile)
        preflight = dict(_json_obj(body.get("preflight")))
        insured_customer = dict(_json_obj(preflight.get("insuredCustomer")))
        if not insured_customer.get("attempted"):
            insured_customer = self._query_insured_customer_by_car_best_effort(client, quote_form)
        quote_form, insured_customer_applied = self._apply_insured_customer_to_quote_form(
            quote_form,
            _json_obj(insured_customer.get("selected")),
        )
        if insured_customer_applied:
            insured_customer = {**insured_customer, "appliedFields": insured_customer_applied}
        preflight.update(
            {
                "selectedVehicle": selected,
                "preciseVehicle": precise_vehicle,
                "actualValue": actual_value_result,
                "insuredCustomer": insured_customer,
                "vehicleModelAutoAccepted": {
                    "accepted": True,
                    "reason": "平台提示车型不一致，已自动使用平台返回车型码重试一次",
                    "platformReturnedCode": selected_code,
                    "vehicleName": quote_form.get("prpCitemCar.brandName"),
                    "vehicleId": quote_form.get("prpCitemCar.modelCode"),
                    "vehicleModelCode": quote_form.get("prpCmain.vehicleModelCode"),
                    "purchasePrice": quote_form.get("prpCitemCar.purchasePrice"),
                    "energyTypePlat": quote_form.get("energyTypePlat"),
                    "energyTypePlatTemp": quote_form.get("energyTypePlatTemp"),
                    "vehicleFuelType": quote_form.get("prpCitemCar.vehicleFuelType"),
                    "selectedBy": _vehicle_selection_rule(explicit_loss_amount),
                    "requestedLossAmount": _clean_money_text(explicit_loss_amount) if explicit_loss_amount > 0 else "",
                    "lossThresholdPurchasePrice": _clean_money_text(_vehicle_loss_threshold(explicit_loss_amount)) if explicit_loss_amount > 0 else "",
                },
            }
        )
        body["vehicleForm"] = _clean_vehicle_cert_fields(vehicle)
        body["productForm"] = {
            "products": products,
            "sharedMainLimit": _checked(_default_value(defaults, PRODUCT_SHARED_LIMIT, True), default=True),
        }
        body["quoteForm"] = _clean_vehicle_cert_fields(quote_form)
        body["preflight"] = preflight
        return _clean_used_fuel_request_body(body), True

    def _quote_sync(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        client: Optional[PiccProtocolClient] = None
        real_account_type = self._real_quote_account_type(ctx, quote_payload)
        is_real_quote = bool(real_account_type)
        request_body_draft: Dict[str, Any] = {}
        draft_error = ""
        request_body: Dict[str, Any] = {}
        prequote_auto_notices: List[Dict[str, Any]] = []
        runtime_stage = "init"
        try:
            request_body_draft = (
                self._assemble_used_fuel_offline_request_body(ctx, quote_payload, account_type_name=real_account_type)
                if is_real_quote
                else self._assemble_untyped_request_draft(ctx, quote_payload)
            )
        except Exception as exc:
            draft_error = str(exc)[:500] or exc.__class__.__name__
            request_body_draft = {
                "platform": "PICC",
                "accountTypeName": ctx.account_type_name or "",
                "offlineDraft": True,
                "draftError": draft_error,
            }
        if not is_real_quote:
            return PlatformRuntimeResult(
                status="failed",
                message="人保报价暂时无法确定车辆类型，不能生成真实报价结果",
                data={
                    "mode": "picc_motor_quote_type_missing",
                    "request_body": request_body_draft,
                    "request_body_draft": request_body_draft,
                    "offline_request_body": True,
                    "request_body_error": draft_error,
                },
            )
        try:
            client = self._client(ctx)
            # 先做一次认证探针，确保当前账号会话仍可用于业务接口。
            runtime_stage = "keepalive"
            data = client.request_json(
                "GET",
                KEEPALIVE_PATH,
                purpose="business",
                params=KEEPALIVE_PARAMS,
                headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
            )
            if isinstance(data, Mapping) and int(data.get("status", -1)) != 0:
                status_code = int(data.get("status", -1))
                runtime_status = "expired" if status_code == 16 else "failed"
                return PlatformRuntimeResult(
                    status=runtime_status,
                    message=str(data.get("statusText") or data),
                    data=success_data(
                        client,
                        extra={
                            "business_status": data.get("status"),
                            "platform_status_text": data.get("statusText") or "",
                            "request_body": request_body_draft,
                            "request_body_draft": request_body_draft,
                            "offline_request_body": True,
                            "request_body_error": draft_error,
                        },
                    ),
                )

            request_body = request_body_draft
            quote_result: Dict[str, Any] = {}
            if is_real_quote:
                runtime_stage = "prepare_quote"
                auto_notice_callback = _json_obj(ctx.payload).get("auto_notice_callback")
                flow_type = _to_str(_json_obj(quote_payload).get("quote_flow_type")).strip()
                if flow_type == "renewal_motor_quote":
                    request_body = self._prepare_renewal_used_fuel_quote(
                        client,
                        ctx,
                        quote_payload,
                        account_type_name=real_account_type,
                    )
                else:
                    request_body = self._prepare_used_fuel_quote(client, ctx, quote_payload, account_type_name=real_account_type)
                duplicate_confirm_payload = _duplicate_quote_confirmation_payload(request_body)
                if duplicate_confirm_payload and not _duplicate_quote_confirmed(quote_payload, request_body):
                    preflight = dict(_json_obj(request_body.get("preflight")))
                    preflight["confirmDuplicateQuote"] = True
                    preflight["duplicateQuoteConfirmed"] = True
                    preflight["allowDuplicateQuote"] = True
                    request_body = {**request_body, "preflight": preflight}
                    notice = _duplicate_quote_auto_notice_from_confirmation_payload(duplicate_confirm_payload)
                    if notice:
                        emitted = _emit_platform_auto_notice(auto_notice_callback, notice)
                        if emitted:
                            notice["emitted_to_chat"] = True
                        prequote_auto_notices.append(notice)
                runtime_stage = "submit_quote"
                request_body, quote_response, auto_period_notices = self._submit_used_fuel_quote_with_period_auto_adjust(
                    client,
                    request_body,
                    auto_notice_callback=auto_notice_callback,
                )
                runtime_stage = "build_quote_result"
                quote_result = self._build_motor_quote_result_from_response(ctx, quote_payload, request_body, quote_response)
                if not _picc_quote_result_has_real_premium(quote_result):
                    failure_auto_notices = [*prequote_auto_notices, *auto_period_notices]
                    _remember_platform_notice_from_quote_response(
                        failure_auto_notices,
                        quote_response,
                        auto_notice_callback=auto_notice_callback,
                    )
                    data_payload: Dict[str, Any] = {
                        "error_code": "quote_result_missing_premium",
                        "error_stage": runtime_stage,
                        "request_body": request_body,
                        "platform_response": _platform_debug_payload(quote_response),
                    }
                    if failure_auto_notices:
                        data_payload["platform_auto_notices"] = [dict(item) for item in failure_auto_notices]
                    return PlatformRuntimeResult(
                        status="failed",
                        message="人保报价接口返回成功状态，但没有返回真实保费明细，未生成报价结果",
                        data=success_data(client, extra=data_payload),
                    )
                platform_auto_notices = [*prequote_auto_notices, *auto_period_notices]
                platform_dialog = _used_fuel_quote_platform_dialog(quote_response)
                platform_dialog_subtype = _to_str(platform_dialog.get("subtype")).strip().lower()
                duplicate_notice = _duplicate_quote_notice_from_success_dialog(
                    platform_dialog,
                    has_period_auto_notice=bool(auto_period_notices),
                    has_duplicate_precheck_notice=any(
                        _to_str(item.get("type")).strip() == "duplicate_quote_notice"
                        for item in prequote_auto_notices
                    ),
                )
                if duplicate_notice:
                    emitted = _emit_platform_auto_notice(auto_notice_callback, duplicate_notice)
                    if emitted:
                        duplicate_notice["emitted_to_chat"] = True
                    platform_auto_notices.append(duplicate_notice)
                if platform_dialog and platform_dialog_subtype != "insurance_date_adjust":
                    _remember_platform_notice_from_quote_response(
                        platform_auto_notices,
                        quote_response,
                        auto_notice_callback=auto_notice_callback,
                    )
                if platform_auto_notices:
                    quote_result["platform_auto_notices"] = platform_auto_notices
                # Platform prompts are emitted as chat notices so the assistant
                # never waits on a frontend dialog for an automatic quote path.
                runtime_stage = "postchecks"
                post_quote = self._run_used_fuel_quote_postchecks(client, request_body, quote_response)
                runtime_stage = "query_usage"
                usage = self._query_quote_times_best_effort(ctx, client=client)
                runtime_stage = "clear_cache"
                cleanup = self._clear_quote_cache_best_effort(client, quote_response)
                quote_result["platform_post_quote"] = post_quote
                quote_result["platform_usage"] = usage
                quote_result["quote_cache_cleanup"] = cleanup
            else:
                post_quote = {"attempted": False, "reason": "当前账号类型未执行真实报价，跳过报价结果页补充查询"}
                usage = {"available": False, "reason": "当前账号类型未执行真实报价，跳过平台次数查询"}
                cleanup = {"attempted": False, "reason": "当前账号类型未执行真实报价，跳过报价缓存清理"}
            return PlatformRuntimeResult(
                status="quoted",
                message="PICC 报价完成",
                data=success_data(
                    client,
                    extra={
                        "mode": quote_result.get("mode") or "picc_motor_real",
                        "request_body": request_body,
                        "quote_result": quote_result,
                        "platform_dialog": quote_result.get("platform_dialog"),
                        "platform_post_quote": post_quote,
                        "platform_usage": usage,
                        "quote_cache_cleanup": cleanup,
                        "platform_status": data.get("status") if isinstance(data, Mapping) else None,
                        "platform_status_text": data.get("statusText") if isinstance(data, Mapping) else "",
                    },
                ),
            )
        except PiccDuplicateQuoteError as exc:
            data_payload = {"business_status": "duplicate_quote", "error_code": "duplicate_quote"}
            if prequote_auto_notices:
                data_payload["platform_auto_notices"] = [dict(item) for item in prequote_auto_notices]
            if client is not None:
                data_payload = success_data(client, extra=data_payload)
            return PlatformRuntimeResult(
                status="duplicate_quote",
                message=str(exc) or "平台提示该车辆已报价过",
                data=data_payload,
            )
        except PiccQuotaFullError as exc:
            data_payload = {"business_status": "quota_full", "error_code": "quota_full"}
            if prequote_auto_notices:
                data_payload["platform_auto_notices"] = [dict(item) for item in prequote_auto_notices]
            if client is not None:
                data_payload = success_data(client, extra=data_payload)
            return PlatformRuntimeResult(
                status="quota_full",
                message=str(exc) or "查询额度已用完",
                data=data_payload,
            )
        except PiccSessionExpiredError as exc:
            data_payload: Dict[str, Any] = {
                "business_status": "16",
                "error_code": exc.__class__.__name__,
                "request_body": request_body or request_body_draft,
                "request_body_draft": request_body_draft,
                "offline_request_body": True,
                "request_body_error": draft_error,
            }
            if prequote_auto_notices:
                data_payload["platform_auto_notices"] = [dict(item) for item in prequote_auto_notices]
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 登录已过期，请重新登录",
                data=data_payload,
            )
        except PiccTransientGatewayError as exc:
            data_payload = {
                "error_code": exc.__class__.__name__,
                "transient": True,
                "request_body": request_body or request_body_draft,
                "request_body_draft": request_body_draft,
                "offline_request_body": True,
                "request_body_error": draft_error,
            }
            if prequote_auto_notices:
                data_payload["platform_auto_notices"] = [dict(item) for item in prequote_auto_notices]
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data=data_payload,
            )
        except PiccRequestError as exc:
            data_payload: Dict[str, Any] = {
                "error_code": exc.__class__.__name__,
                "error_stage": getattr(exc, "action", "") or runtime_stage,
                "request_body": request_body or request_body_draft,
                "request_body_draft": request_body_draft,
                "offline_request_body": True,
                "request_body_error": draft_error,
            }
            if isinstance(exc, PiccBusinessRequestError):
                data_payload["platform_response"] = _platform_debug_payload(getattr(exc, "platform_response", None))
                data_payload["platform_dialog"] = _platform_business_error_dialog(getattr(exc, "platform_response", None))
            auto_notices = [
                *prequote_auto_notices,
                *[
                    dict(item or {})
                    for item in getattr(exc, "platform_auto_notices", None) or []
                    if isinstance(item, Mapping)
                ],
            ]
            if auto_notices:
                data_payload["platform_auto_notices"] = auto_notices
            if isinstance(exc, PiccBusinessRequestError):
                data_payload["request_body_envelope"] = data_payload["request_body"]
                data_payload["request_form_body"] = getattr(exc, "request_body", None) or {}
                data_payload["request_body"] = data_payload["request_form_body"] or data_payload["request_body"]
            if client is not None:
                data_payload = success_data(client, extra=data_payload)
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data=data_payload,
            )
        except Exception as exc:
            data_payload = {
                "error_code": exc.__class__.__name__,
                "error_stage": runtime_stage,
                "request_body": request_body or request_body_draft,
                "request_body_draft": request_body_draft,
                "offline_request_body": True,
                "request_body_error": draft_error,
            }
            if prequote_auto_notices:
                data_payload["platform_auto_notices"] = [dict(item) for item in prequote_auto_notices]
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data=data_payload,
            )

    def _query_quote_times_best_effort(
        self,
        ctx: PlatformAccountContext,
        *,
        client: Optional[PiccProtocolClient] = None,
    ) -> Dict[str, Any]:
        try:
            query_client = client or self._client(ctx)
            data = query_client.request_json(
                "GET",
                QUERY_QUOTE_TIMES_PATH,
                purpose="business",
                headers={"Referer": f"{query_client.config.base_url}/khyxui/my-tools/quotation"},
            )
            return _quote_times_payload(data)
        except PiccSessionExpiredError as exc:
            return {
                "available": False,
                "today_used_count": None,
                "source": "queryQuoteTimes",
                "error_code": "session_expired",
                "message": str(exc) or "PICC 登录已过期，请重新登录",
            }
        except Exception as exc:
            return {
                "available": False,
                "today_used_count": None,
                "source": "queryQuoteTimes",
                "error_code": exc.__class__.__name__,
                "message": str(exc)[:300] or exc.__class__.__name__,
            }

    def _clear_quote_cache_best_effort(
        self,
        client: PiccProtocolClient,
        quote_response: Mapping[str, Any],
    ) -> Dict[str, Any]:
        data = _json_obj(_json_obj(quote_response).get("data"))
        quotation_id = _to_str(data.get("quotationId")).strip()
        if not quotation_id:
            return {"attempted": False, "success": False, "reason": "平台未返回报价流水号，跳过报价缓存清理"}

        risk_codes = [
            risk_code
            for risk_code, field_name in JOINT_SALES_QUOTATION_FIELDS.items()
            if _to_str(data.get(field_name)).strip()
        ]
        if not risk_codes:
            # 抓包页面里该接口主要用于清联合销售缓存；无险种时只做一次保守兜底清理。
            risk_codes = [""]

        calls: List[Dict[str, Any]] = []
        for risk_code in risk_codes:
            params: Dict[str, Any] = {"quotationId": quotation_id}
            if risk_code:
                params["riskCode"] = risk_code
            try:
                result = client.request_json(
                    "GET",
                    CLEAR_JS_QUOTATION_NO_PATH,
                    purpose="business",
                    params=params,
                    headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
                )
                calls.append(
                    {
                        "risk_code": risk_code or "",
                        "success": _platform_status_code(result) == 0,
                        "platform_status": _platform_status_code(result),
                        "platform_status_text": _platform_message(result, ""),
                    }
                )
            except Exception as exc:
                calls.append(
                    {
                        "risk_code": risk_code or "",
                        "success": False,
                        "error_code": exc.__class__.__name__,
                        "message": str(exc)[:300] or exc.__class__.__name__,
                    }
                )
        return {
            "attempted": True,
            "success": bool(calls) and all(bool(item.get("success")) for item in calls),
            "quotation_id": quotation_id,
            "calls": calls,
        }

    def _run_used_fuel_quote_postchecks(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        quote_response: Mapping[str, Any],
    ) -> Dict[str, Any]:
        return {
            "attempted": True,
            "qualityFlag": self._query_quality_flag_best_effort(client, request_body, quote_response),
            "clubGiftDisplay": self._query_club_gift_display_best_effort(client, request_body, quote_response),
        }

    def _query_quality_flag_best_effort(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        quote_response: Mapping[str, Any],
    ) -> Dict[str, Any]:
        data = _json_obj(_json_obj(quote_response).get("data"))
        form = _json_obj(_json_obj(request_body).get("quoteForm"))
        vin_no = _clean_vehicle_cert_value("vin", _first_text(data.get("vinNo"), data.get("frameNo"), form.get("prpCitemCar.vinNo")))
        quotation_id = _first_text(data.get("quotationId"), form.get("quotationId"))
        if not vin_no or not quotation_id:
            return {"attempted": False, "reason": "平台未返回车架号或报价流水号，跳过质量标记查询"}
        try:
            result = client.request_json(
                "GET",
                QUERY_QUALITY_FLAG_PATH,
                purpose="business",
                params={"vinNo": vin_no, "quotationId": quotation_id},
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            return {
                "attempted": True,
                "success": _platform_status_code(result) == 0,
                "platform_status": _platform_status_code(result),
                "platform_status_text": _platform_message(result, ""),
                "data": _json_obj(result).get("data"),
            }
        except Exception as exc:
            return {
                "attempted": True,
                "success": False,
                "error_code": exc.__class__.__name__,
                "message": str(exc)[:300] or exc.__class__.__name__,
            }

    def _query_club_gift_display_best_effort(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        quote_response: Mapping[str, Any],
    ) -> Dict[str, Any]:
        data = _json_obj(_json_obj(quote_response).get("data"))
        body = _json_obj(request_body)
        form = _json_obj(body.get("quoteForm"))
        vehicle = _json_obj(body.get("vehicleForm"))
        kinds = data.get("itemKindTempList")
        quotation_no = _first_text(data.get("quotationNo"), form.get("quotationNo"))
        if not isinstance(kinds, list) or not kinds or not quotation_no:
            return {"attempted": False, "reason": "平台未返回险种明细或报价单号，跳过礼包展示查询"}

        params = {
            "premiumBI": _first_text(data.get("biPremium"), data.get("premiumBI")),
            "netPremiumBI": _first_text(data.get("binetPremium"), data.get("netPremiumBI")),
            "lastDamagedBI": _first_text(data.get("lastDamagedBI"), "0"),
            "premiumCI": _first_text(data.get("ciPremium"), data.get("premiumCI")),
            "netPremiumCI": _first_text(data.get("cinetPremium"), data.get("netPremiumCI")),
            "lastDamagedCI": _first_text(data.get("lastDamagedCI"), "0"),
            "isSameInsurance": _first_text(data.get("isSameInsurance")),
            "insuredVehicleBrand": _first_text(
                data.get("insuredVehicleBrand"),
                form.get("insuredVehicleBrand"),
                form.get("prpCitemCar.searchseqno"),
                vehicle.get("vehicleFgwCode"),
                vehicle.get("modelCode"),
                data.get("modelCodeDAA"),
                data.get("modelCodeDZA"),
            ),
            "vehicleUsage": _first_text(form.get("prpCitemCar.useNatureCode"), vehicle.get("useNatureCode"), "211"),
            "kinds": json.dumps(kinds, ensure_ascii=False, separators=(",", ":")),
            "useYears": _first_text(form.get("prpCitemCar.useYears"), _use_years(vehicle.get("enrollDate")), "0"),
            "selectedOperateConfigId": _first_text(form.get("selectedOperateConfigId"), form.get("selectedOperateConfig")),
            "licenseNo": _first_text(data.get("licenseNo"), form.get("prpCitemCar.licenseNo"), vehicle.get("licenseNo")),
            "startDateBI": _first_text(form.get("prpCmain.startDate"), vehicle.get("startDateBI")),
            "startDateCI": _first_text(form.get("prpCmain.startDateCI"), vehicle.get("startDateCI")),
            "projectCode": _first_text(data.get("projectCode"), form.get("projectCode"), "0"),
            "monopolyCode": _first_text(form.get("monopolyCode")),
            "noDamYearsBI": _first_text(data.get("noDamYearsBI"), "0"),
            "noDamYearsCI": _first_text(data.get("noDamYearsCI"), "0"),
            "seatCount": _first_text(form.get("prpCitemCar.seatCount"), vehicle.get("seatCount"), "5"),
            "piccScore": _first_text(data.get("piccScore")),
            "piccscorestarlevel": _first_text(data.get("piccscorestarlevel")),
            "quotationNo": quotation_no,
            "opconfComCode": _first_text(form.get("opconfComCode"), form.get("opconf_makecomCode")),
            "giftPackageDconfigValue": _first_text(data.get("giftPackageDconfigValue"), form.get("giftPackageDconfigValue")),
        }
        try:
            result = client.request_json(
                "GET",
                GET_CLUB_GIFT_DISPLAY_INFO_PATH,
                purpose="business",
                params=params,
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            return {
                "attempted": True,
                "success": _platform_status_code(result) == 0,
                "platform_status": _platform_status_code(result),
                "platform_status_text": _platform_message(result, ""),
                "data": _json_obj(result).get("data"),
            }
        except Exception as exc:
            return {
                "attempted": True,
                "success": False,
                "error_code": exc.__class__.__name__,
                "message": str(exc)[:300] or exc.__class__.__name__,
            }

    def _assemble_untyped_request_draft(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = _json_obj(quote_payload)
        request_body = _json_obj(payload.get("request_body"))
        default_config = _json_obj(payload.get("default_config_json"))
        normalized_data = _json_obj(payload.get("normalized_data"))
        body = _deep_merge(
            {
                "requestId": f"picc-{uuid.uuid4().hex}",
                "platform": "PICC",
                "accountTypeName": ctx.account_type_name or "",
                "vehicle": {},
                "applicant": {},
                "defaults": default_config,
            },
            request_body,
        )
        body["vehicle"] = _deep_merge(
            _json_obj(body.get("vehicle")),
            {
                "plateNo": normalized_data.get("plate_no"),
                "vin": normalized_data.get("vin"),
                "engineNo": normalized_data.get("engine_no"),
                "vehicleModel": normalized_data.get("vehicle_model"),
            },
        )
        body["vehicle"] = _clean_vehicle_cert_fields(_json_obj(body.get("vehicle")))
        body["applicant"] = _deep_merge(
            _json_obj(body.get("applicant")),
            {
                "name": normalized_data.get("owner_name") or normalized_data.get("id_name"),
                "phone": normalized_data.get("owner_phone"),
                "idNo": normalized_data.get("id_number"),
            },
        )
        body["applicant"] = _clean_vehicle_cert_fields(_json_obj(body.get("applicant")))
        return body

    def _real_quote_account_type(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> str:
        payload = _json_obj(quote_payload)
        detected = _json_obj(payload.get("vehicle_type_detect"))
        platform_default = _json_obj(payload.get("platform_default_config"))
        candidates = (
            platform_default.get("resolved_type_name"),
            platform_default.get("account_type_name"),
            detected.get("config_type_name"),
            ctx.account_type_name,
        )
        for value in candidates:
            normalized = _normalize_account_type(value)
            if normalized in PICC_REAL_QUOTE_ACCOUNT_TYPES:
                return normalized
        return ""

    def _is_used_fuel_quote(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> bool:
        return self._real_quote_account_type(ctx, quote_payload) == USED_FUEL_ACCOUNT_TYPE

    def _used_fuel_owner(self, defaults: Mapping[str, Any], normalized_data: Mapping[str, Any]) -> Dict[str, Any]:
        owner = {
            "ownerName": _first_text(normalized_data.get("owner_name"), normalized_data.get("id_name"), _field_value(defaults, "车主")),
            "ownerIdNo": _first_text(normalized_data.get("id_number"), _field_value(defaults, "车主证件号码")),
            "ownerPhone": _first_text(normalized_data.get("owner_phone"), _field_value(defaults, "车主手机号")),
        }
        return _clean_vehicle_cert_fields(owner)

    def _assemble_used_fuel_offline_request_body(
        self,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        *,
        account_type_name: str = USED_FUEL_ACCOUNT_TYPE,
    ) -> Dict[str, Any]:
        """Build an auditable used-fuel request draft before touching the platform session."""
        profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        resolved_account_type = _profile_text(profile, "account_type_name", USED_FUEL_ACCOUNT_TYPE)
        payload = _json_obj(quote_payload)
        defaults = _picc_business_defaults(payload.get("default_config_json"))
        normalized_data = _json_obj(payload.get("normalized_data"))
        explicit_loss_amount = _quote_loss_override_amount(payload, defaults)
        incoming_body = _json_obj(payload.get("request_body"))
        incoming_preflight = _json_obj(incoming_body.get("preflight"))

        vehicle = _deep_merge(
            self._base_used_fuel_vehicle(defaults, normalized_data, profile=profile),
            _json_obj(incoming_body.get("vehicleForm")),
        )
        vehicle = _clean_vehicle_cert_fields(vehicle)
        _apply_vehicle_model_seed_hints(
            vehicle,
            _json_obj(_json_obj(normalized_data.get("renewal_request_body_seed")).get("vehicleForm")),
            _json_obj(incoming_body.get("vehicleForm")),
        )
        owner = _deep_merge(
            self._used_fuel_owner(defaults, normalized_data),
            _json_obj(incoming_body.get("ownerForm")),
        )
        owner = _clean_vehicle_cert_fields(owner)
        selected = _json_obj(
            incoming_body.get("selectedVehicle")
            or incoming_preflight.get("selectedVehicle")
            or incoming_preflight.get("vehicleSearchSelected")
        )
        precise_vehicle = _json_obj(incoming_body.get("preciseVehicle") or incoming_preflight.get("preciseVehicle"))
        actual_value = _first_text(
            vehicle.get("actualValue"),
            _profile_product_default(defaults, profile, PRODUCT_LOSS),
            _field_value(defaults, "机动车损失保险", "车损险"),
            vehicle.get("purchasePrice"),
            _field_value(defaults, "新车购置价", "购置价", "purchasePrice"),
            "0",
        )
        if not _to_str(vehicle.get("actualValue")).strip():
            vehicle["actualValue"] = _money_text(actual_value)
        if not _to_str(vehicle.get("purchasePrice")).strip():
            purchase_price = _first_text(_field_value(defaults, "新车购置价", "购置价", "purchasePrice"))
            if purchase_price:
                vehicle["purchasePrice"] = _clean_money_text(purchase_price)

        products = self._used_fuel_products(defaults, profile=profile, actual_value=actual_value, seat_count=vehicle.get("seatCount"))
        joint_sale = _tujia_anshun_config(defaults)
        quote_form_error = ""
        try:
            quote_form = self._build_used_fuel_quote_form(
                defaults,
                vehicle,
                owner,
                selected,
                precise_vehicle,
                products,
                profile=profile,
                strict_config=False,
            )
        except Exception as exc:
            quote_form = {}
            quote_form_error = str(exc)[:500] or exc.__class__.__name__

        missing_config = []
        for label, aliases in (
            ("归属机构代码", ("归属机构代码", "机构代码", "comCode")),
            ("操作机构代码", ("操作机构代码", "出单机构代码", "opconfComCode", "opconf_makecomCode")),
            ("操作配置ID", ("操作配置ID", "车型配置ID", "selectedOperateConfigId")),
            ("验车人工号", ("验车人工号", "验车人代码", "carcheckerCode")),
        ):
            if not _to_str(_field_value(defaults, *aliases)).strip():
                missing_config.append(label)

        return _clean_used_fuel_request_body({
            "requestId": f"{_profile_text(profile, 'request_id_prefix', 'picc-motor')}-draft-{uuid.uuid4().hex}",
            "platform": "PICC",
            "accountTypeName": resolved_account_type,
            "offlineDraft": True,
            "draftReason": "平台会话校验前生成，用于掉线/登录过期时审计已解析资料和待提交参数",
            "vehicleForm": vehicle,
            "ownerForm": owner,
            "productForm": {
                "products": products,
                "sharedMainLimit": _checked(_default_value(defaults, PRODUCT_SHARED_LIMIT, True), default=True),
            },
            "jointSaleForm": {
                "tujiaAnshun": {
                    **joint_sale,
                    "offline": True,
                },
            },
            "quoteForm": quote_form,
            "defaultFields": defaults,
            "preflight": {
                "vehicleSearch": {
                    "candidateCount": None,
                    "selectedBy": _vehicle_selection_rule(explicit_loss_amount),
                    "selectedPrice": vehicle.get("purchasePrice"),
                    "requestedLossAmount": _clean_money_text(explicit_loss_amount) if explicit_loss_amount > 0 else "",
                    "lossThresholdPurchasePrice": _clean_money_text(_vehicle_loss_threshold(explicit_loss_amount)) if explicit_loss_amount > 0 else "",
                    "reason": (
                        "当前为离线草稿，登录恢复后会重新查询车型列表；"
                        "未在会话输入车损时同分车型取最低购置价，已输入车损时按车损/1.3匹配最近的不低于阈值车型"
                    ),
                },
                "preciseVehicle": precise_vehicle,
                "energyResolve": {
                    "energyTypePlat": quote_form.get("energyTypePlat"),
                    "energyTypePlatTemp": quote_form.get("energyTypePlatTemp"),
                    "vehicleEnergyType": quote_form.get("prpCitemCar.energyType"),
                    "isEnergyCar": quote_form.get("prpCitemCar.isEnergyCar"),
                    "vehicleFuelType": quote_form.get("prpCitemCar.vehicleFuelType"),
                },
                "licenseResolve": {
                    "licenseType": vehicle.get("licenseType") or quote_form.get("prpCitemCar.licenseType"),
                    "licenseColorCode": vehicle.get("licenseColorCode") or quote_form.get("prpCitemCar.licenseColorCode"),
                    "decision": _json_obj(vehicle.get("license_type_decision")),
                },
                "actualValue": {"offline": True, "value": _money_text(actual_value)},
                "selectedVehicle": selected,
                "quoteFormError": quote_form_error,
                "missingDefaultConfig": missing_config,
                "jointSale": {
                    "tujiaAnshun": {
                        **joint_sale,
                        "offline": True,
                    },
                },
            },
        })

    def _prepare_used_fuel_quote(
        self,
        client: PiccProtocolClient,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        *,
        account_type_name: str = USED_FUEL_ACCOUNT_TYPE,
    ) -> Dict[str, Any]:
        profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        resolved_account_type = _profile_text(profile, "account_type_name", USED_FUEL_ACCOUNT_TYPE)
        payload = _json_obj(quote_payload)
        defaults = _picc_business_defaults(payload.get("default_config_json"))
        normalized_data = _json_obj(payload.get("normalized_data"))
        explicit_loss_amount = _quote_loss_override_amount(payload, defaults)
        incoming_body = _json_obj(payload.get("request_body"))
        vehicle = self._base_used_fuel_vehicle(defaults, normalized_data, profile=profile)
        _apply_vehicle_model_seed_hints(
            vehicle,
            _json_obj(_json_obj(normalized_data.get("renewal_request_body_seed")).get("vehicleForm")),
            _json_obj(incoming_body.get("vehicleForm")),
        )
        owner = self._used_fuel_owner(defaults, normalized_data)

        search_result = self._query_vehicle_candidates(client, vehicle, profile=profile, defaults=defaults)
        candidates = _vehicle_rows(search_result)
        selected = _pick_best_vehicle_candidate(candidates, vehicle, explicit_loss_amount=explicit_loss_amount)
        if not selected:
            tried_terms = [
                text
                for item in (vehicle.get("modelQueryTerms") if isinstance(vehicle.get("modelQueryTerms"), list) else [])
                for text in [_to_str(item).strip().rstrip("*")]
                if text
            ]
            raise PiccRequestError(_vehicle_model_resolution_failure_message(vehicle, tried_terms))

        precise_result = self._query_precise_vehicle(client, vehicle, defaults, selected, profile=profile)
        precise_rows = _vehicle_rows(precise_result)
        precise_vehicle = (
            _pick_best_vehicle_candidate(precise_rows, vehicle, explicit_loss_amount=explicit_loss_amount)
            if precise_rows
            else {}
        )
        actual_value_result = self._query_actual_value(client, vehicle, defaults, selected, precise_vehicle, profile=profile)
        platform_purchase_price = _vehicle_platform_purchase_price(selected, precise_vehicle)
        actual_value = _actual_value_from_response(actual_value_result, platform_purchase_price or selected.get("actualValue"))
        if _profile_text(profile, "new_car_flag") and platform_purchase_price:
            actual_value = platform_purchase_price

        selected_price = _money(platform_purchase_price)
        search_code = _model_search_code(_vehicle_platform_fgw_code(vehicle, selected, precise_vehicle))
        taxabate_result = {}
        if search_code:
            try:
                taxabate_result = client.request_json(
                    "GET",
                    TAXABATE_QUERY_PATH,
                    purpose="business",
                    params={"searchCode": search_code},
                    headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
                )
            except Exception as exc:
                taxabate_result = {"skipped": True, "message": str(exc)[:300]}

        vehicle.update(
            {
                "purchasePrice": str(int(selected_price)) if selected_price == selected_price.to_integral() else str(selected_price),
                "actualValue": _money_text(actual_value),
                "modelCode": _first_text(selected.get("vehicleId"), selected.get("modelCode"), precise_vehicle.get("vehicleId")),
                "platModelCode": _first_text(precise_vehicle.get("platModelCode"), selected.get("platModelCode")),
                "selectedModelName": _first_text(selected.get("vehicleName"), precise_vehicle.get("vehicleName"), vehicle.get("modelName")),
                "selectedVehicleAlias": _first_text(selected.get("vehicleAlias"), precise_vehicle.get("vehicleAlias")),
                "selectedVehicleId": _first_text(selected.get("vehicleId"), precise_vehicle.get("vehicleId")),
                "vehicleFgwCode": search_code,
                "platformBrandId": _vehicle_platform_brand_id(selected, precise_vehicle),
                "platformBrandIDNew": _vehicle_platform_brand_id_new(
                    selected,
                    precise_vehicle,
                    _vehicle_platform_brand_id(selected, precise_vehicle),
                ),
            }
        )
        vehicle = _clean_vehicle_cert_fields(vehicle)
        checker_info = self._query_car_checker(client, defaults)
        if checker_info:
            vehicle["carchecker"] = _first_text(checker_info.get("userName"), vehicle.get("carchecker"))
            vehicle["mainComCode"] = _first_text(checker_info.get("comCode"), vehicle.get("mainComCode"))
        products = self._used_fuel_products(defaults, profile=profile, actual_value=actual_value, seat_count=vehicle.get("seatCount"))
        quote_form = self._build_used_fuel_quote_form(defaults, vehicle, owner, selected, precise_vehicle, products, profile=profile)
        insured_customer = self._query_insured_customer_by_car_best_effort(client, quote_form)
        quote_form, insured_customer_applied = self._apply_insured_customer_to_quote_form(
            quote_form,
            _json_obj(insured_customer.get("selected")),
        )
        if insured_customer_applied:
            insured_customer = {**insured_customer, "appliedFields": insured_customer_applied}
        prechecks = self._run_used_fuel_quote_prechecks(client, defaults, quote_form)
        joint_sale = self._query_tujia_anshun_plan_best_effort(client, defaults, quote_form)

        return _clean_used_fuel_request_body({
            "requestId": f"{_profile_text(profile, 'request_id_prefix', 'picc-motor')}-{uuid.uuid4().hex}",
            "platform": "PICC",
            "accountTypeName": resolved_account_type,
            "vehicleForm": vehicle,
            "ownerForm": owner,
            "productForm": {
                "products": products,
                "sharedMainLimit": _checked(_default_value(defaults, PRODUCT_SHARED_LIMIT, True), default=True),
            },
            "jointSaleForm": {
                "tujiaAnshun": joint_sale,
            },
            "quoteForm": quote_form,
            "defaultFields": defaults,
            "preflight": {
                "vehicleSearch": {
                    "candidateCount": len(candidates),
                    "selectedBy": _vehicle_selection_rule(explicit_loss_amount),
                    "selectedPrice": vehicle.get("purchasePrice"),
                    "selectedScore": _vehicle_candidate_score(selected, vehicle),
                    "modelQueryMatched": vehicle.get("modelQueryMatched") or "",
                    "modelQueryMatchKind": vehicle.get("modelQueryMatchKind") or "",
                    "modelQueryMatchLabel": vehicle.get("modelQueryMatchLabel") or "",
                    "vehicleQueryResourcesUsed": vehicle.get("vehicleQueryResourcesUsed") or "",
                    "vehicleQueryResourcesTried": list(vehicle.get("vehicleQueryResourcesTried") or [])[:8],
                    "modelQueryTerms": list(vehicle.get("modelQueryTerms") or [])[:12],
                    "requestedLossAmount": _clean_money_text(explicit_loss_amount) if explicit_loss_amount > 0 else "",
                    "lossThresholdPurchasePrice": _clean_money_text(_vehicle_loss_threshold(explicit_loss_amount)) if explicit_loss_amount > 0 else "",
                },
                "selectedVehicle": selected,
                "preciseVehicle": precise_vehicle,
                "energyResolve": {
                    "energyTypePlat": quote_form.get("energyTypePlat"),
                    "energyTypePlatTemp": quote_form.get("energyTypePlatTemp"),
                    "vehicleEnergyType": quote_form.get("prpCitemCar.energyType"),
                    "isEnergyCar": quote_form.get("prpCitemCar.isEnergyCar"),
                    "vehicleFuelType": quote_form.get("prpCitemCar.vehicleFuelType"),
                },
                "licenseResolve": {
                    "licenseType": vehicle.get("licenseType") or quote_form.get("prpCitemCar.licenseType"),
                    "licenseColorCode": vehicle.get("licenseColorCode") or quote_form.get("prpCitemCar.licenseColorCode"),
                    "decision": _json_obj(vehicle.get("license_type_decision")),
                },
                "actualValue": actual_value_result,
                "taxabate": taxabate_result,
                "insuredCustomer": insured_customer,
                "carChecker": checker_info,
                "quotePrechecks": prechecks,
                "jointSale": {
                    "tujiaAnshun": joint_sale,
                },
            },
        })

    def _query_tujia_anshun_plan_best_effort(
        self,
        client: PiccProtocolClient,
        defaults: Mapping[str, Any],
        quote_form: Mapping[str, Any],
    ) -> Dict[str, Any]:
        config = _tujia_anshun_config(defaults)
        if not config.get("enabled"):
            return {
                **config,
                "attempted": False,
                "success": True,
                "risk_code": PICC_TUJIA_ANSHUN_RISK_CODE,
                "brand_id": PICC_TUJIA_ANSHUN_BRAND_ID,
                "service_group_type_code": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
            }
        monopoly_code = _to_str(quote_form.get("monopolyCode") or _repair_code_value(defaults)).strip()
        try:
            data = client.request_json(
                "GET",
                JOINT_SALE_PLAN_INFO_PATH,
                purpose="business",
                params={
                    "riskCode": PICC_TUJIA_ANSHUN_RISK_CODE,
                    "brandId": PICC_TUJIA_ANSHUN_BRAND_ID,
                    "serviceGroupTypeCode": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
                    "monopolyCode": monopoly_code,
                },
                headers={
                    "Referer": f"{client.config.base_url}/khyxui/my-tools/quotation",
                },
            )
            _ensure_platform_success(data, action="途家安顺保额查询")
            rows = _plan_rows_from_response(data)
            selected = _pick_joint_sale_plan_by_premium(data, config.get("premium"))
            if not selected:
                return {
                    **config,
                    "attempted": True,
                    "success": False,
                    "amount": "0",
                    "candidate_count": len(rows),
                    "match_count": 0,
                    "risk_code": PICC_TUJIA_ANSHUN_RISK_CODE,
                    "brand_id": PICC_TUJIA_ANSHUN_BRAND_ID,
                    "service_group_type_code": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
                    "message": f"未查询到保费为{config.get('premium')}的途家安顺方案",
                }
            amount = _clean_money_text(selected.get("planAmount"), "0")
            premium = _clean_money_text(selected.get("planPremium"), _to_str(config.get("premium") or "398"))
            return {
                **config,
                "attempted": True,
                "success": True,
                "premium": premium,
                "amount": amount,
                "candidate_count": len(rows),
                "match_count": _safe_int_local(selected.get("_matchCount"), 1),
                "risk_code": PICC_TUJIA_ANSHUN_RISK_CODE,
                "brand_id": PICC_TUJIA_ANSHUN_BRAND_ID,
                "service_group_type_code": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
                "selected_plan": {
                    "planName": _to_str(selected.get("planName")).strip(),
                    "planCode": _to_str(selected.get("planCode")).strip(),
                    "planPremium": premium,
                    "planAmount": amount,
                },
                "selection_rule": "同保费多方案时选择保额最高方案",
            }
        except Exception as exc:
            return {
                **config,
                "attempted": True,
                "success": False,
                "amount": "0",
                "risk_code": PICC_TUJIA_ANSHUN_RISK_CODE,
                "brand_id": PICC_TUJIA_ANSHUN_BRAND_ID,
                "service_group_type_code": PICC_TUJIA_ANSHUN_SERVICE_GROUP_TYPE_CODE,
                "error_code": exc.__class__.__name__,
                "message": str(exc)[:300] or exc.__class__.__name__,
            }

    def _query_car_checker(self, client: PiccProtocolClient, defaults: Mapping[str, Any]) -> Dict[str, Any]:
        checker_code = _to_str(
            _field_value(defaults, "验车人工号", "验车人代码", "carcheckerCode", "carCheckerCode")
        ).strip()
        opconf_com_code = _to_str(
            _field_value(defaults, "操作机构代码", "出单机构代码", "opconfComCode", "opconf_makecomCode")
        ).strip()
        if not checker_code or not opconf_com_code:
            return {}
        data = client.request_json(
            "GET",
            QUERY_CAR_CHECKER_PATH,
            purpose="business",
            params={
                "otherCondition": f"selectCarChecker,comCode={opconf_com_code}",
                "fieldValue": checker_code,
                "callbackCarChecker": 1,
                "hdzyFlag": 1,
            },
            headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
        )
        _ensure_platform_success(data, action="验车人查询")
        rows = _json_obj(_json_obj(data).get("data")).get("list")
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            return dict(rows[0])
        return {}

    def _query_insured_customer_by_car_best_effort(
        self,
        client: PiccProtocolClient,
        quote_form: Mapping[str, Any],
    ) -> Dict[str, Any]:
        vin = _to_str(_clean_vehicle_cert_value("vin", quote_form.get("prpCitemCar.vinNo") or quote_form.get("prpCitemCar.frameNo"))).strip()
        license_no = _to_str(quote_form.get("prpCitemCar.licenseNo")).strip()
        if not vin or not license_no:
            return {
                "attempted": False,
                "found": False,
                "reason": "missing_vin_or_license_no",
            }
        params = {
            "carQuotationReqBody.vinNo": vin,
            "carQuotationReqBody.licenseNo": license_no,
            "carQuotationReqBody.toDoJumpFlag": "0",
            "carQuotationReqBody.requestType": "1",
        }
        try:
            data = client.request_json(
                "GET",
                QUERY_INSURED_BY_CAR_INFO_PATH,
                purpose="business",
                params=params,
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            payload = _json_obj(_json_obj(data).get("data"))
            rows = payload.get("insuredList")
            customers = [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
            return {
                "attempted": True,
                "found": bool(customers),
                "params": params,
                "count": len(customers),
                "status": _json_obj(data).get("status"),
                "statusText": _json_obj(data).get("statusText") or "",
                "selected": customers[0] if customers else {},
            }
        except Exception as exc:
            return {
                "attempted": True,
                "found": False,
                "params": params,
                "error_code": exc.__class__.__name__,
                "message": str(exc)[:300] or exc.__class__.__name__,
            }

    @staticmethod
    def _apply_insured_customer_to_quote_form(
        quote_form: Mapping[str, Any],
        customer: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], List[str]]:
        form = dict(quote_form)
        row = _json_obj(customer)
        if not row:
            return form, []

        applied: List[str] = []

        def put(key: str, value: Any, *, overwrite: bool = True) -> None:
            if value is None:
                return
            if isinstance(value, (Mapping, list, tuple, set)):
                return
            text = _to_str(value).strip()
            if text == "":
                return
            if not overwrite and _to_str(form.get(key)).strip():
                return
            if form.get(key) != text:
                applied.append(key)
            form[key] = text

        # HAR shows the page carrying the ECIF customer row as khyxCinsured[0].*
        # and using the same row to fill owner/insured birthday, sex and age.
        for key, value in row.items():
            put(f"khyxCinsured[0].{key}", value)

        identify_number = row.get("identifyNumber")
        put("lastIdentifyNo", identify_number)

        insured_type = _first_text(row.get("insuredType"), "1")
        birthday = row.get("birthday")
        age = row.get("age")
        sex = row.get("sex")

        for prefix in ("quoteInsured", "quoteCarOwner"):
            put(f"{prefix}.insuredType", insured_type)
            put(f"{prefix}.birthday", birthday)
            put(f"{prefix}.age", age)
            put(f"{prefix}.sex", sex)

        name = row.get("insuredName")
        put("carOwner", name, overwrite=False)
        put("prpCcarShipTax.remark1", name, overwrite=False)
        return form, applied

    def _run_used_fuel_quote_prechecks(
        self,
        client: PiccProtocolClient,
        defaults: Mapping[str, Any],
        quote_form: Mapping[str, Any],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        quote_form = _clean_vehicle_cert_fields(quote_form)
        selected_operate_config_id = _to_str(
            quote_form.get("selectedOperateConfigId") or _field_value(defaults, "操作配置ID", "车型配置ID", "selectedOperateConfigId")
        ).strip()
        checker_code = _to_str(quote_form.get("carcheckerCode") or _field_value(defaults, "验车人工号", "验车人代码", "carcheckerCode")).strip()
        monopoly_code = _to_str(quote_form.get("monopolyCode") or _repair_code_value(defaults)).strip()
        if selected_operate_config_id and checker_code:
            verify_data = client.request_json(
                "GET",
                VERIFY_AGENT_CONTROL_PATH,
                purpose="business",
                params={
                    "selectedOperateConfigId": selected_operate_config_id,
                    "monopolyCode": monopoly_code,
                    "carCheckerCode": checker_code,
                },
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            _ensure_platform_success(verify_data, action="代理配置校验")
            out["agentControl"] = _json_obj(verify_data).get("data") or {}

        vin = _to_str(_clean_vehicle_cert_value("vin", quote_form.get("prpCitemCar.vinNo"))).strip()
        if vin:
            duplicate_data = client.request_json(
                "GET",
                DUPLICATE_INSURED_VIN_PATH,
                purpose="business",
                params={"vinNo": vin},
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            _ensure_platform_success(duplicate_data, action="重复车辆校验")
            duplicate_payload = _json_obj(_json_obj(duplicate_data).get("data"))
            duplicate_warning = _duplicate_insured_vin_warning(vin, duplicate_payload)
            out["duplicateVin"] = {
                "total": duplicate_payload.get("total", 0),
                "message": duplicate_warning,
                "list": duplicate_payload.get("list") if isinstance(duplicate_payload.get("list"), list) else [],
            }
            if _safe_int_local(duplicate_payload.get("total"), 0) > 0:
                out["duplicateVin"]["warning"] = duplicate_warning or "平台返回已有历史记录，本次继续提交并以最终报价接口结果为准"
        return out

    def _build_used_fuel_quote_form(
        self,
        defaults: Mapping[str, Any],
        vehicle: Mapping[str, Any],
        owner: Mapping[str, Any],
        selected: Mapping[str, Any],
        precise_vehicle: Mapping[str, Any],
        products: List[Dict[str, Any]],
        *,
        profile: Optional[Mapping[str, Any]] = None,
        strict_config: bool = True,
    ) -> Dict[str, Any]:
        prof = _json_obj(profile)
        seats = max(1, _safe_int_local(vehicle.get("seatCount"), 5))
        passenger_quantity = max(seats - 1, 1)
        start_date_bi = _first_text(vehicle.get("startDateBI"), _next_day_text())
        start_date_ci = _first_text(vehicle.get("startDateCI"), _next_day_text())
        start_hour_bi, start_minute_bi = _period_time_texts(vehicle.get("startHourBI"), vehicle.get("startMinuteBI"))
        start_hour_ci, start_minute_ci = _period_time_texts(vehicle.get("startHourCI"), vehicle.get("startMinuteCI"))
        end_date_ci = _ci_end_date_text(start_date_ci, start_hour_ci, start_minute_ci)
        end_hour_ci = "24" if start_hour_ci == "0" and start_minute_ci == "0" else start_hour_ci
        owner_name = _to_str(owner.get("ownerName")).strip()
        selected_operate_config_id = _to_str(
            _field_value(defaults, "操作配置ID", "车型配置ID", "selectedOperateConfigId")
        ).strip()
        opconf_com_code = _to_str(
            _field_value(defaults, "操作机构代码", "出单机构代码", "opconfComCode", "opconf_makecomCode")
        ).strip()
        carchecker_code = _to_str(_field_value(defaults, "验车人工号", "验车人代码", "carcheckerCode")).strip()
        carchecker_name = _first_text(vehicle.get("carchecker"), _field_value(defaults, "验车人姓名", "验车人", "carCeckName"))
        repair_code_enabled = _repair_code_enabled(defaults)
        repair_code = _repair_code_value(defaults)
        repair_name = _repair_code_name(defaults)
        main_com_code = _first_text(vehicle.get("mainComCode"), _field_value(defaults, "归属机构代码", "机构代码", "comCode"))
        query_area = _first_text(_field_value(defaults, "查询区域代码", "queryArea"), (main_com_code[:2] + "0000") if len(main_com_code) >= 2 else "")
        model_code = _first_text(
            vehicle.get("platformModelCode"),
            vehicle.get("modelCode"),
            selected.get("vehicleId"),
            selected.get("modelCode"),
            precise_vehicle.get("vehicleId"),
        )
        vehicle_model_code = _first_text(
            vehicle.get("platformVehicleModelCode"),
            precise_vehicle.get("platModelCode"),
            selected.get("vehicleModelCode"),
            vehicle.get("platModelCode"),
        )
        platform_purchase_price = _vehicle_platform_purchase_price(selected, precise_vehicle)
        purchase_price = _clean_money_text(_first_text(platform_purchase_price, vehicle.get("purchasePrice")), "0")
        actual_value = _money_text(_first_text(purchase_price if _profile_text(prof, "new_car_flag") else "", vehicle.get("actualValue")))
        brand_id = _first_text(vehicle.get("platformBrandId"), _vehicle_platform_brand_id(selected, precise_vehicle))
        brand_id_new = _first_text(
            vehicle.get("platformBrandIDNew"),
            _vehicle_platform_brand_id_new(selected, precise_vehicle, brand_id),
        )
        vehicle_fgw_code = _vehicle_platform_fgw_code(vehicle, selected, precise_vehicle)
        search_code = _vehicle_platform_search_seqno(selected, brand_id, vehicle_fgw_code)
        ton_count = _first_text(precise_vehicle.get("tonCount"), selected.get("tonCount"), selected.get("vehicleTonnage"))
        if _money(ton_count) == 0:
            ton_count = ""
        energy_fields = _resolve_vehicle_energy_fields(defaults, selected, precise_vehicle, vehicle=vehicle, profile=prof)
        license_fields = _profile_license_fields(prof, energy_fields, defaults, vehicle=vehicle)
        tax_fields = _profile_tax_field_values(prof, energy_fields, defaults, start_date_ci)
        resolved_license_type = license_fields["license_type"]
        resolved_license_color_code = license_fields["license_color_code"]
        local_use_flag = _checked_flag_text(
            _field_value(defaults, "本地使用", "是否本地使用", "localUse", "prpCitemCar.localUse", fallback="1"),
            default=True,
        )
        local_license_flag = _checked_flag_text(
            _field_value(defaults, "本地上牌", "是否本地上牌", "localLicense", "prpCitemCar.localLicense", fallback="1"),
            default=True,
        )

        compulsory_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_COMPULSORY, "20"), "20")
        loss_amount = _money_text(_first_text(_profile_product_default(defaults, prof, PRODUCT_LOSS), actual_value))
        third_party_config = _profile_product_default(defaults, prof, PRODUCT_THIRD_PARTY, "300")
        third_party_amount = _wan_or_amount_to_wan_text(third_party_config, "300")
        driver_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_DRIVER, "2"), "2")
        passenger_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_PASSENGER, "2"), "2")
        shared_main_limit = _checked(_profile_product_default(defaults, prof, PRODUCT_SHARED_LIMIT, True), default=True)
        medical_third_amount = _wan_or_amount_to_amount(
            _profile_product_default(defaults, prof, PRODUCT_MEDICAL_THIRD, third_party_config),
            third_party_amount or "300",
        )
        if shared_main_limit:
            medical_third_amount = _wan_or_amount_to_amount(third_party_amount, third_party_amount or "300")
        road_rescue_quantity = _safe_int_local(_default_value(defaults, PRODUCT_ROAD_RESCUE, ""), 0)

        missing_config = []
        if not main_com_code:
            missing_config.append("归属机构代码")
        if not opconf_com_code:
            missing_config.append("操作机构代码")
        if not selected_operate_config_id:
            missing_config.append("操作配置ID")
        if not carchecker_code:
            missing_config.append("验车人工号")
        if missing_config and strict_config:
            raise PiccRequestError(
                f"{_profile_text(prof, 'display_name', '人保报价')}缺少平台默认参数："
                + "、".join(missing_config)
                + "。请先在报价助手右上角“默认参数配置”中维护后再报价。"
            )

        form: Dict[str, Any] = {
            "prpCmain.comCode": main_com_code,
            "prpCmain.startDate": start_date_bi,
            "prpCmain.starthourbi": start_hour_bi,
            "prpCmain.startminutebi": start_minute_bi,
            "prpCmain.endhourbi": "24",
            "prpCmain.endminutebi": "0",
            "prpCmain.startDateCI": start_date_ci,
            "prpCmain.starthourci": start_hour_ci,
            "prpCmain.startminuteci": start_minute_ci,
            "prpCmain.endDateCI": end_date_ci,
            "prpCmain.endhourci": end_hour_ci,
            "prpCmain.endminuteci": start_minute_ci,
            "prpCmain.custAuthorization": "0",
            "prpCmain.vehicleModelCode": vehicle_model_code,
            "prpCmain.insuredChooseUsedName": "0",
            "prpCmain.carOwnerChooseUsedName": "0",
            "businesNature": _to_str(_field_value(defaults, "业务性质代码", "businesNature", fallback="2")),
            "businesNatureName": _to_str(_field_value(defaults, "业务性质名称", "businesNatureName", fallback="专业代理业务")),
            "energyTypePlat": energy_fields["energy_type_plat"],
            "energyTypePlatTemp": energy_fields["energy_type_name"],
            "eadinfo.isEAD": "0",
            "transfer": "0",
            "transferDate": _date_text(vehicle.get("transferDate")),
            "renewed": "0",
            "groupCodeValidStatus": "1" if repair_code_enabled and repair_code else "0",
            "monopolyCode": repair_code if repair_code_enabled else "",
            "monopolyName": repair_name if repair_code_enabled else "",
            "HNfeProjectCode": "0",
            "isNetTransProposal": "0",
            "opVehicleTaxFlag": _to_str(_field_value(defaults, "是否代收车船税", "opVehicleTaxFlag", fallback="1")),
            "carShipTaxTJFlag": "0",
            "netTPPrpCyelInfo.insuredYELCopies": "1",
            "prpCitemCar.searchseqno": search_code,
            "prpCitemCar.Nodamageyears": main_com_code,
            "prpCitemCar.carchecker": carchecker_name,
            "prpCitemCar.licenseNo": _to_str(vehicle.get("licenseNo")),
            "prpCitemCar.licenseType": resolved_license_type,
            "prpCitemCar.engineNo": _to_str(vehicle.get("engineNo")),
            "prpCitemCar.vinNo": _to_str(vehicle.get("vin")),
            "prpCitemCar.frameNo": _to_str(vehicle.get("vin")),
            "prpCitemCar.carKindCode": _to_str(vehicle.get("carKindCode") or "A01"),
            "prpCitemCar.useNatureCode": _to_str(vehicle.get("useNatureCode") or "211"),
            "prpCitemCar.enrollDate": _to_str(vehicle.get("enrollDate")),
            "prpCitemCar.useYears": _use_years(vehicle.get("enrollDate")),
            "prpCitemCar.brandName": _first_text(
                vehicle.get("selectedModelName"),
                precise_vehicle.get("vehicleName"),
                selected.get("vehicleName"),
                vehicle.get("modelName"),
            ),
            "prpCitemCar.brandId": brand_id,
            "prpCitemCar.brandIDNew": brand_id_new,
            "prpCitemCar.vehicleMaker": _first_text(selected.get("vehicleMaker"), precise_vehicle.get("vehicleMakerid"), precise_vehicle.get("vehicleMaker")),
            "prpCitemCar.familyId": _first_text(selected.get("familyId"), vehicle.get("familyId")),
            "prpCitemCar.modelDemandNo": model_code,
            "prpCitemCar.modelCode": model_code,
            "prpCitemCar.purchasePrice": purchase_price,
            "prpCitemCar.actualValue": actual_value,
            "prpCitemCar.seatCount": str(seats),
            "prpCitemCar.tonCount": ton_count,
            "prpCitemCar.countryNature": _to_str(_field_value(defaults, "国产进口标识", "countryNature", fallback="01")),
            "prpCitemCar.exhaustScale": _first_text(selected.get("vehicleExhaust"), precise_vehicle.get("vehicleExhaust")),
            "prpCitemCar.carLotEquQuality": _first_text(precise_vehicle.get("vehicleWeight"), selected.get("vehicleWeight")),
            "prpCitemCar.enginePower": _first_text(selected.get("enginePower"), precise_vehicle.get("enginePower")),
            "prpCitemCar.runAreaCode": _to_str(_field_value(defaults, "行驶区域代码", "runAreaCode", fallback="11")),
            "prpCitemCar.energyType": energy_fields["vehicle_energy_type"],
            "prpCitemCar.referenceActualValue": actual_value,
            "prpCitemCar.queryArea": query_area,
            "prpCitemCar.carInsuredRelation": _to_str(_field_value(defaults, "车主与被保险人关系", "carInsuredRelation", fallback="所有")),
            "prpCitemCar.loanVehicleFlag": "0",
            "prpCitemCar.clauseType": _to_str(_field_value(defaults, "条款类型", "clauseType", fallback="F42")),
            "prpCitemCar.licenseColorCode": resolved_license_color_code,
            "prpCitemCar.netWeifaFlag": "0",
            "prpCitemCar.isEnergyCar": energy_fields["is_energy_car"],
            "prpCitemCar.isDangerousCar": "0",
            "prpCitemCar.IsCriterion": "1",
            "prpCitemCar.localUse": local_use_flag,
            "prpCitemCar.localLicense": local_license_flag,
            "prpCitemCar.taxPayerType": _to_str(_field_value(defaults, "纳税人类型", "taxPayerType", fallback="01")),
            "prpCitemCar.fuelType": energy_fields["fuel_type"],
            "prpCitemCar.vehicleFuelType": energy_fields["vehicle_fuel_type"],
            "prpCitemCar.vehicleFgwCode": vehicle_fgw_code,
            "prpCitemCar.colorCode": _to_str(_field_value(defaults, "车辆颜色代码", "colorCode", fallback="999")),
            "prpCitemCar.nonlocalFlag": "0",
            "selectedOperateConfigId": selected_operate_config_id,
            "selectedOperateConfig": selected_operate_config_id,
            "selectedNetOperateConfigId": _to_str(_field_value(defaults, "网络操作配置ID", "selectedNetOperateConfigId")),
            "netSaleTest": "0",
            "skipFeeReformFlag": "1",
            "netClaimFlag": "0",
            "quotePageId": str(uuid.uuid4()),
            "source": "1",
            "carcheckerCode": carchecker_code,
            "carcheckstatus": "1",
            "carchecktime": _today_text(),
            "agriflag": "0",
            "carOwner": owner_name,
            "quoteCarOwner.insuredType": _to_str(_field_value(defaults, "车主类型", "quoteCarOwner.insuredType", fallback="1")),
            "quoteCarOwner.sex": _to_str(_field_value(defaults, "车主性别", "quoteCarOwner.sex", fallback="1")),
            "quoteCarOwner.birthday": _date_text(_field_value(defaults, "车主生日", "quoteCarOwner.birthday")) or "1990-01-01",
            "prpCitemKindsTemp[1].kindCode": "051050",
            "prpCitemKindsTemp[1].kindName": PRODUCT_LOSS,
            "prpCitemKindsTemp[1].deductible": "0",
            "riskCodeType": "DZA",
            "quantity": str(passenger_quantity),
            "prpCcarShipTax.taxType": _to_str(_field_value(defaults, "车船税类型", "taxType", fallback=tax_fields["tax_type"])),
            "prpCcarShipTax.calculateMode": _to_str(_field_value(defaults, "车船税计算方式", "calculateMode", fallback=tax_fields["calculate_mode"])),
            "prpCcarShipTax.taxcomcode": _to_str(_field_value(defaults, "税务机关代码", "taxcomcode")),
            "prpCcarShipTax.taxcomname": _to_str(_field_value(defaults, "税务机关名称", "taxcomname")),
            "prpCcarShipTax.taxAbateType": _to_str(_field_value(defaults, "车船税减免类型", "taxAbateType", fallback=tax_fields["tax_abate_type"])),
            "prpCcarShipTax.taxAbateReason": _to_str(_field_value(defaults, "车船税减免原因", "taxAbateReason", fallback=tax_fields["tax_abate_reason"])),
            "prpCcarShipTax.dutyPaidProofNo": _to_str(_field_value(defaults, "完税证明号", "dutyPaidProofNo", fallback=tax_fields["duty_paid_proof_no"])),
            "prpCcarShipTax.payStartDate": _to_str(_field_value(defaults, "车船税起始日期", "payStartDate", fallback=tax_fields["pay_start_date"])),
            "prpCcarShipTax.payEndDate": _to_str(_field_value(defaults, "车船税终止日期", "payEndDate", fallback=tax_fields["pay_end_date"])),
            "prpCcarShipTax.payLastYear": _period_last_year(start_date_ci),
            "prpCcarShipTax.taxregistrynumber": _to_str(_field_value(defaults, "车船税纳税人识别号", "taxregistrynumber")),
            "prpCcarShipTax.remark1": owner_name,
            "prpCcarShipTax.leviedDate": _today_text(),
            "isSendCriterion": "0",
            "carQuoteInsuredRealList[0].serialNo": "0",
            "carQuoteInsuredRealList[0].holdIdentifyType": "01",
            "carQuoteInsuredRealList[0].holdType": "01",
            "carQuoteInsuredRealList[1].serialNo": "1",
            "carQuoteInsuredRealList[1].holdIdentifyType": "01",
            "carQuoteInsuredRealList[1].holdType": "02",
            "carQuoteInsuredRealList[2].serialNo": "2",
            "carQuoteInsuredRealList[2].holdIdentifyType": "01",
            "carQuoteInsuredRealList[2].holdType": "03",
            "opconfComCode": opconf_com_code,
            "carCeckName": carchecker_name,
            "autoGetChacker": "0",
            "deviceList[0].serialno": "1",
            "softWareEquipments[0].serialNo": "1",
            "hardWareEquipments[0].serialNo": "1",
            "vehicleStyleFlag": "0",
            "prpCmain.vehicleStyleUniqueId": _first_text(selected.get("vehicleStyleUniqueId"), vehicle.get("vehicleStyleUniqueId")),
            "prpCmain.presaleCarFlag": _first_text(precise_vehicle.get("presaleCarFlag"), selected.get("presaleCarFlag")),
            "firstQuote": "0",
            "notBindingFlag": "0",
            "energyFlag": energy_fields["energy_flag"],
            "quoteCacheFlagVal": "on",
            "opconf_makecomCode": opconf_com_code,
        }
        if _profile_text(prof, "new_car_flag"):
            form["newCarFlag"] = _profile_text(prof, "new_car_flag")
        if not _profile_bool(prof, "include_pay_last_year", True):
            form.pop("prpCcarShipTax.payLastYear", None)
        if not _to_str(form.get("prpCitemCar.familyId")).strip():
            form.pop("prpCitemCar.familyId", None)
        if _profile_text(prof, "is_energy_car") == "1" and _money(form.get("prpCitemCar.exhaustScale")) == 0:
            form.pop("prpCitemCar.exhaustScale", None)
        external_grid_amount = _external_grid_amount(defaults, actual_value)
        product_specs: List[Dict[str, Any]] = [
            {"amount": compulsory_amount, "kind_code": "051074", "kind_name": PRODUCT_COMPULSORY},
            {"amount": loss_amount, "kind_code": "051050", "kind_name": PRODUCT_LOSS},
            {"amount": third_party_amount, "kind_code": "051051", "kind_name": PRODUCT_THIRD_PARTY},
            {"amount": driver_amount, "kind_code": "051052", "kind_name": PRODUCT_DRIVER},
            {"amount": passenger_amount, "kind_code": "051053", "kind_name": PRODUCT_PASSENGER},
            {"amount": medical_third_amount, "kind_code": "051063", "kind_name": PRODUCT_MEDICAL_THIRD},
        ]
        if road_rescue_quantity > 0:
            product_specs.append(
                {
                    "amount": "",
                    "kind_code": "051064",
                    "kind_name": PRODUCT_ROAD_RESCUE,
                    "quantity": str(road_rescue_quantity),
                }
            )
        if _is_positive_amount(external_grid_amount):
            product_specs.append(
                {
                    "amount": external_grid_amount,
                    "kind_code": "051085",
                    "kind_name": PRODUCT_EXTERNAL_GRID,
                }
            )
        excluded_products = _product_exclusions(defaults)
        product_rows = [
            spec
            for spec in product_specs
            if _canonical_product_name(spec.get("kind_name")) not in excluded_products
        ]
        medical_third_index: Optional[int] = None
        for index, spec in enumerate(product_rows):
            amount = _to_str(spec.get("amount")).strip()
            kind_code = _to_str(spec.get("kind_code")).strip()
            kind_name = _to_str(spec.get("kind_name")).strip()
            form[f"prpCitemKindVos[{index}].amount"] = amount
            form[f"prpCitemKindVos[{index}].kindCode"] = kind_code
            form[f"prpCitemKindVos[{index}].kindName"] = kind_name
            form[f"prpCitemKindVos[{index}].chooseFlag"] = "true"
            quantity = _to_str(spec.get("quantity")).strip()
            if quantity:
                form[f"prpCitemKindVos[{index}].quantity"] = quantity
            if kind_name == PRODUCT_MEDICAL_THIRD:
                medical_third_index = index
        if medical_third_index is not None:
            form[f"prpCitemKindVos[{medical_third_index}].sharedAmountFlag"] = "1" if shared_main_limit else "0"
        for key in USED_FUEL_QUOTE_EMPTY_FORM_FIELDS:
            form.setdefault(key, "")
        # Allow advanced platform-specific overrides without changing the schema.
        extra_form = _json_obj_loose(defaults.get("PICC报价请求体覆盖") or defaults.get("quoteFormOverrides"))
        for key, value in extra_form.items():
            form_key = _to_str(key).strip()
            if not form_key:
                continue
            if _is_dangerous_quote_form_override_key(form_key):
                raise PiccRequestError(f"人保报价请求体覆盖包含危险字段：{form_key}，请改用会话调参或默认参数配置")
            form[form_key] = value
        form = _clean_vehicle_cert_fields(form)
        return {key: value for key, value in form.items() if value is not None}

    def _validate_picc_quote_form_before_submit(
        self,
        form: Mapping[str, Any],
        *,
        account_type_name: Any = "",
        flow_type: Any = "",
    ) -> None:
        del flow_type
        required_amount_kind_codes = {
            "051051": "第三者责任险",
            "051052": PRODUCT_DRIVER,
            "051053": PRODUCT_PASSENGER,
            "051063": PRODUCT_MEDICAL_THIRD,
        }
        problems: List[str] = []
        for row in _quote_form_kind_rows(form):
            kind_code = _to_str(row.get("kind_code")).strip()
            if not _checked(row.get("choose_flag"), default=False):
                continue
            if kind_code in required_amount_kind_codes and not _is_positive_amount(row.get("amount")):
                problems.append(f"{required_amount_kind_codes[kind_code]}已选择，但保额无效")
            if kind_code == "051064" and _safe_int_local(row.get("quantity"), 0) <= 0:
                problems.append(f"{PRODUCT_ROAD_RESCUE}已选择，但次数无效")

        normalized_type = _normalize_account_type(account_type_name)
        license_type = _normalize_license_type_value(form.get("prpCitemCar.licenseType"))
        energy_flag = _to_str(form.get("energyFlag")).strip()
        is_energy_car = _to_str(form.get("prpCitemCar.isEnergyCar")).strip()
        energy_type_plat = _to_str(form.get("energyTypePlat")).strip()
        if "新能源" in normalized_type:
            if license_type and license_type != "52":
                problems.append("新能源旧车号牌种类不是52")
            if energy_flag and energy_flag != "1":
                problems.append("新能源旧车energyFlag不是1")
            if is_energy_car and is_energy_car not in {"1", "true", "True"}:
                problems.append("新能源旧车isEnergyCar不是1")
        elif normalized_type in {USED_FUEL_ACCOUNT_TYPE, NEW_FUEL_ACCOUNT_TYPE}:
            if license_type == "52":
                problems.append("燃油车号牌种类不能是52")
        if problems:
            raise PiccRequestError("人保报价前校验失败：" + "；".join(problems[:8]))

    def _submit_used_fuel_quote(self, client: PiccProtocolClient, request_body: Mapping[str, Any]) -> Dict[str, Any]:
        form_body = _clean_vehicle_cert_fields(_json_obj(request_body.get("quoteForm")))
        if not form_body:
            raise PiccRequestError("人保报价请求体为空，无法提交报价")
        self._validate_picc_quote_form_before_submit(
            form_body,
            account_type_name=request_body.get("accountTypeName"),
            flow_type=request_body.get("quoteFlowType") or request_body.get("quote_flow_type"),
        )
        data = client.request_json(
            "POST",
            QUOTE_PATH,
            purpose="business",
            form_body=form_body,
            headers={
                "Origin": client.config.base_url,
                "Referer": f"{client.config.base_url}/khyxui/my-tools/quotation",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        status_code = _platform_status_code(data)
        if status_code != 0:
            message = _platform_message(data, "平台返回业务校验失败")
            if _quote_response_has_display_result(data):
                return _json_obj(data)
            # PICC can label a period-adjustment response as "duplicate
            # insurance". It is not a duplicate-quote stop: preserve the raw
            # response so the bounded retry loop can change the correct dates.
            if _platform_response_requires_insurance_date_adjustment(data):
                raise PiccBusinessRequestError(
                    f"报价提交失败：{message}",
                    action="报价提交",
                    platform_response=data,
                    request_body=form_body,
                )
            if _contains_duplicate_quote(data):
                raise PiccDuplicateQuoteError(message or "平台提示该车辆已报价过")
            if _contains_quota_full(data):
                raise PiccQuotaFullError(message or "查询额度已用完")
            raise PiccBusinessRequestError(
                f"报价提交失败：{message}",
                action="报价提交",
                platform_response=data,
                request_body=form_body,
            )
        payload = _json_obj(_json_obj(data).get("data"))
        if _platform_response_requires_insurance_date_adjustment(data):
            raise PiccBusinessRequestError(
                f"报价提交失败：{_platform_message(data, '平台提示需要修改保险期间')}",
                action="报价提交",
                platform_response=data,
                request_body=form_body,
            )
        if _contains_duplicate_quote(payload) and not _to_str(payload.get("quotationNo")).strip():
            raise PiccDuplicateQuoteError(_platform_message(data, "平台提示该车辆已报价过"))
        return _json_obj(data)

    def _base_used_fuel_vehicle(
        self,
        defaults: Mapping[str, Any],
        data: Mapping[str, Any],
        *,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        prof = _json_obj(profile)
        next_day = _next_day_text()
        vehicle_type = _first_text(data.get("vehicle_type"), _field_value(defaults, "车辆类型"))
        raw_model_name = _first_text(data.get("vehicle_model"))
        vehicle_brand_name = _first_text(data.get("vehicle_brand_name"))
        vehicle_name_hint = _first_text(
            data.get("vehicle_name"),
            data.get("car_name"),
        )
        engine_no = _first_text(data.get("engine_no"), _field_value(defaults, "发动机号"))
        vin = _first_text(data.get("vin"), _field_value(defaults, "VIN/车架号", "车架号"))
        license_no = _first_text(data.get("plate_no"), _field_value(defaults, "号牌号码"))
        engine_no = _clean_vehicle_cert_value("engine_no", engine_no)
        vin = _clean_vehicle_cert_value("vin", vin)
        license_no = _clean_vehicle_cert_value("plate_no", license_no)
        if not license_no and _profile_text(prof, "license_no_strategy") == "new_car_placeholder":
            license_no = _new_car_placeholder_license(engine_no, vin)
        transfer_date = ""
        if _checked(data.get("is_transfer_vehicle"), default=False):
            transfer_date = _date_text(_first_text(data.get("transfer_date"), data.get("issue_date")))
        enroll_date = _first_text(
            _date_text(data.get("first_register_date")),
            _date_text(data.get("issue_date")),
            _date_text(_field_value(defaults, "初登日期")),
        )
        if not enroll_date and _profile_text(prof, "enroll_date_fallback") == "today":
            enroll_date = _today_text()
        energy_model_suffix = _vehicle_energy_model_suffix(data, prof)
        initial_energy_fields = _resolve_vehicle_energy_fields(
            defaults,
            {},
            {},
            vehicle={
                "rawModelName": raw_model_name,
                "vehicleType": vehicle_type,
                "brandNameHint": vehicle_brand_name,
                "vehicleNameHint": vehicle_name_hint,
                "energyModelSuffix": energy_model_suffix,
            },
            profile=prof,
        )
        license_fields = _profile_license_fields(
            prof,
            initial_energy_fields,
            defaults,
            vehicle={
                "license_type_decision": data.get("license_type_decision"),
                "licenseType": data.get("license_type") or data.get("licenseType"),
                "licenseColorCode": data.get("license_color_code") or data.get("licenseColorCode"),
            },
        )
        model_terms = _used_fuel_model_query_terms(
            raw_model_name,
            vehicle_type,
            energy_model_suffix,
            brand_name=vehicle_brand_name,
            vehicle_name=vehicle_name_hint,
            vin=vin,
        )
        vehicle = {
            "licenseNo": license_no,
            "licenseType": license_fields["license_type"],
            "licenseColorCode": license_fields["license_color_code"],
            "license_type_decision": _json_obj(data.get("license_type_decision")),
            "engineNo": engine_no,
            "vin": vin,
            "transferDate": transfer_date,
            "carKindCode": _first_text(_field_value(defaults, "车辆种类"), "A01"),
            "useNatureCode": _first_text(_field_value(defaults, "使用性质细分种类", "使用性质"), "211"),
            "enrollDate": enroll_date,
            "startDateBI": _first_text(_date_text(data.get("commercial_start_date")), _date_text(_field_value(defaults, "商业起保日期")), next_day),
            "startDateCI": _first_text(_date_text(data.get("compulsory_start_date")), _date_text(_field_value(defaults, "交强起保日期")), next_day),
            "startHourBI": _first_text(data.get("commercial_start_hour"), data.get("startHourBI"), _field_value(defaults, "商业起保小时"), "0"),
            "startMinuteBI": _first_text(data.get("commercial_start_minute"), data.get("startMinuteBI"), _field_value(defaults, "商业起保分钟"), "0"),
            "startHourCI": _first_text(data.get("compulsory_start_hour"), data.get("startHourCI"), _field_value(defaults, "交强起保小时", "交强险起保小时"), "0"),
            "startMinuteCI": _first_text(data.get("compulsory_start_minute"), data.get("startMinuteCI"), _field_value(defaults, "交强起保分钟", "交强险起保分钟"), "0"),
            "modelName": model_terms[0] if model_terms else raw_model_name,
            "rawModelName": raw_model_name,
            "modelQueryTerms": model_terms,
            "brandNameHint": vehicle_brand_name,
            "vehicleNameHint": vehicle_name_hint,
            "vehicleType": vehicle_type,
            "energyModelSuffix": energy_model_suffix,
            "seatCount": _first_text(data.get("approved_passenger_count"), _field_value(defaults, "座位数"), "5"),
        }
        return _clean_vehicle_cert_fields(vehicle)

    def _query_vehicle_candidates(
        self,
        client: PiccProtocolClient,
        vehicle: Mapping[str, Any],
        *,
        profile: Optional[Mapping[str, Any]] = None,
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        cleaned_vehicle = _clean_vehicle_cert_fields(vehicle)
        if isinstance(vehicle, dict):
            vehicle.update(cleaned_vehicle)
        else:
            vehicle = cleaned_vehicle
        model_name = _to_str(vehicle.get("rawModelName") or vehicle.get("modelName")).strip()
        if not model_name:
            raise PiccRequestError("人保报价缺少车型名称，无法查询车型配置")
        # Always rebuild terms from current vehicle fields. Stale modelQueryTerms on
        # draft/retry bodies previously poisoned brand-only searches.
        seed_terms = [
            term
            for term in (
                vehicle.get("trustedModelSeedTerms")
                if isinstance(vehicle.get("trustedModelSeedTerms"), list)
                else []
            )
            if _vehicle_model_hint_is_usable(vehicle, term)
            and not _vehicle_term_lacks_sales_specificity(term, vehicle)
        ]
        rebuilt_terms = _dedupe_model_terms(
            _used_fuel_model_query_terms(
                model_name,
                vehicle.get("vehicleType"),
                vehicle.get("energyModelSuffix"),
                brand_name=vehicle.get("brandNameHint"),
                vehicle_name=vehicle.get("vehicleNameHint"),
                vin=vehicle.get("vin"),
            )
        ) or [model_name]
        current_model_is_specific = _vehicle_model_hint_is_usable(
            vehicle,
            model_name,
        ) and not _vehicle_term_lacks_sales_specificity(model_name, vehicle)
        terms = _dedupe_model_terms(
            [*rebuilt_terms, *seed_terms]
            if current_model_is_specific
            else [*seed_terms, *rebuilt_terms]
        )
        resource_codes = _vehicle_query_resource_codes(profile=profile, defaults=defaults, vehicle=vehicle)
        if isinstance(vehicle, dict):
            vehicle["modelQueryTerms"] = terms
            vehicle["vehicleQueryResourcesTried"] = list(resource_codes)
            vehicle.pop("modelQueryBlockReason", None)
        last_data: Any = {}
        attempted_terms: set[str] = set()
        has_vin_prefixes = bool(_vehicle_vin_model_code_candidates(vehicle.get("vin")))
        has_usable_sales_hint = _vehicle_model_hint_is_usable(vehicle, vehicle.get("vehicleNameHint"))

        def _request_term(term: str, *, rows_limit: int) -> Any:
            last_local: Any = {}
            for resource_code in resource_codes:
                params = {
                    "jyVehicleRequest.resources": resource_code,
                    "jyVehicleRequest.brandName": "",
                    "jyVehicleRequest.vinno": vehicle.get("vin") or "",
                    "jyVehicleRequest.vehicleName": term if term.endswith("*") else f"{term}*",
                    "jyVehicleRequest.vehicleAlias": "",
                    "jyVehicleRequest.vehicleId": "",
                    "jyVehicleRequest.searchCode": "",
                    "jyVehicleRequest.platModelCode": "",
                    "page": 1,
                    "rows": rows_limit,
                }
                data = client.request_json(
                    "GET",
                    VEHICLE_QUERY_PATH,
                    purpose="business",
                    params=params,
                    headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
                )
                last_local = data
                if isinstance(vehicle, dict):
                    vehicle["vehicleQueryResourcesUsed"] = resource_code
                try:
                    _ensure_platform_success(data, action="车型配置查询")
                except PiccBusinessRequestError:
                    if _is_vehicle_query_no_data_response(data):
                        continue
                    raise
                if _vehicle_rows(data):
                    return data
            return last_local

        for term in terms:
            term_key = _compact_vehicle_compare_text(term)
            if not term_key or term_key in attempted_terms:
                continue
            attempted_terms.add(term_key)
            data = _request_term(term, rows_limit=10)
            last_data = data
            try:
                _ensure_platform_success(data, action="车型配置查询")
            except PiccBusinessRequestError:
                if _is_vehicle_query_no_data_response(data):
                    continue
                raise
            rows = _vehicle_rows(data)
            if not rows:
                continue
            needs_vin_correlation = _vehicle_query_term_requires_vin_correlation(term, vehicle)
            lacks_sales_specificity = _vehicle_term_lacks_sales_specificity(term, vehicle)
            if needs_vin_correlation or (lacks_sales_specificity and not has_usable_sales_hint):
                if not has_vin_prefixes:
                    # Platform may have returned brand catalogue rows, but accepting
                    # them without VIN correlation or a sales-model hint is unsafe.
                    if isinstance(vehicle, dict):
                        vehicle["modelQueryBlockReason"] = (
                            "broad_brand_without_vin"
                            if needs_vin_correlation
                            else "brand_only_without_vin_or_sales_model"
                        )
                    continue
                correlated_rows = _vehicle_rows_correlated_to_vin(rows, vehicle)
                if not correlated_rows:
                    continue
                correlated_data = dict(_json_obj(data))
                correlated_data["result"] = correlated_rows
                if isinstance(vehicle, dict):
                    vehicle["modelName"] = term
                    vehicle["modelQueryTerms"] = terms
                    _apply_vehicle_query_match_meta(
                        vehicle,
                        term,
                        vin_correlated=True,
                        resources=vehicle.get("vehicleQueryResourcesUsed"),
                    )
                    vehicle.pop("modelQueryBlockReason", None)
                return correlated_data
            if isinstance(vehicle, dict):
                vehicle["modelName"] = term
                vehicle["modelQueryTerms"] = terms
                _apply_vehicle_query_match_meta(
                    vehicle,
                    term,
                    vin_correlated=False,
                    resources=vehicle.get("vehicleQueryResourcesUsed"),
                )
                vehicle.pop("modelQueryBlockReason", None)
            return data

        # A VIN/VDS prefix is useful for correlating catalogue rows, but it is
        # not itself a sales model. When OCR only produced a brand plus that
        # prefix, make one bounded broad brand lookup and accept rows only if
        # the platform exposes the same prefix in a model/code field.
        brand = _vehicle_brand_hint(vehicle)
        vin_prefixes = _vehicle_vin_model_code_candidates(vehicle.get("vin"))
        if brand and vin_prefixes:
            suffix = _vehicle_model_suffix_from_type(vehicle.get("vehicleType"))
            broad_terms = _dedupe_model_terms(
                [
                    brand,
                    f"{brand}{suffix}" if suffix else "",
                ]
            )
            for term in broad_terms:
                term_key = _compact_vehicle_compare_text(term)
                if not term_key or term_key in attempted_terms:
                    continue
                attempted_terms.add(term_key)
                data = _request_term(term, rows_limit=100)
                try:
                    _ensure_platform_success(data, action="车型配置查询")
                except PiccBusinessRequestError:
                    if _is_vehicle_query_no_data_response(data):
                        continue
                    raise
                broad_rows = _vehicle_rows(data)
                correlated_rows = _vehicle_rows_correlated_to_vin(broad_rows, vehicle)
                if correlated_rows:
                    correlated_data = dict(_json_obj(data))
                    correlated_data["result"] = correlated_rows
                    if isinstance(vehicle, dict):
                        _apply_vehicle_query_match_meta(
                            vehicle,
                            term,
                            vin_correlated=True,
                            resources=vehicle.get("vehicleQueryResourcesUsed"),
                        )
                        vehicle["modelQueryTerms"] = terms
                        vehicle.pop("modelQueryBlockReason", None)
                    return correlated_data

        # Do not return unrelated broad-search rows. The caller must stop with
        # a clear model-resolution error rather than silently selecting another
        # vehicle from the same brand.
        last_data = dict(_json_obj(last_data))
        last_data["result"] = []
        if isinstance(vehicle, dict):
            vehicle["modelQueryTerms"] = terms
        return last_data

    def _query_precise_vehicle(
        self,
        client: PiccProtocolClient,
        vehicle: Mapping[str, Any],
        defaults: Mapping[str, Any],
        selected: Mapping[str, Any],
        *,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        vehicle = _clean_vehicle_cert_fields(vehicle)
        prof = _json_obj(profile)
        energy_fields = _resolve_vehicle_energy_fields(defaults, selected, {}, vehicle=vehicle, profile=prof)
        license_fields = _profile_license_fields(prof, energy_fields, defaults, vehicle=vehicle)
        purchase_price = _first_text(selected.get("purchasePrice"), selected.get("priceP"), selected.get("priceT"))
        params = {
            "vin": vehicle.get("vin") or "",
            "startDate": vehicle.get("startDateBI") or _next_day_text(),
            "startHour": 0,
            "startMinute": 0,
            "licenseNo": vehicle.get("licenseNo") or "",
            "licenseType": vehicle.get("licenseType") or license_fields["license_type"],
            "carKindCode": vehicle.get("carKindCode") or _first_text(selected.get("vehicleClassPicc"), "A01"),
            "engineNo": vehicle.get("engineNo") or "",
            "enrollDate": vehicle.get("enrollDate") or "",
            "useNatureCode": vehicle.get("useNatureCode") or "211",
            "purchasePrice": purchase_price,
            "modelCode": _first_text(selected.get("vehicleId"), selected.get("modelCode")),
            "tonCount": _first_text(selected.get("tonCount"), selected.get("vehicleTonnage"), "0"),
            "exhaustScale": _first_text(selected.get("vehicleExhaust"), selected.get("exhaustScale"), "0"),
            "selectedOperateConfigId": _field_value(defaults, "车型配置ID", "selectedOperateConfigId"),
            "seatCount": _first_text(selected.get("vehicleSeat"), vehicle.get("seatCount"), "5"),
            "carLotEquQuality": _first_text(selected.get("vehicleWeight"), selected.get("carLotEquQuality"), "0"),
            "clauseFlag": _field_value(defaults, "条款标识", fallback="1"),
            "energyTypePlat": energy_fields["energy_type_plat"],
            "rows": 10,
        }
        data = client.request_json(
            "GET",
            PRECISE_VEHICLE_QUERY_PATH,
            purpose="business",
            params=params,
            headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
        )
        _ensure_platform_success(data, action="精确车型确认")
        return data

    def _query_actual_value(
        self,
        client: PiccProtocolClient,
        vehicle: Mapping[str, Any],
        defaults: Mapping[str, Any],
        selected: Mapping[str, Any],
        precise_vehicle: Mapping[str, Any],
        *,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        vehicle = _clean_vehicle_cert_fields(vehicle)
        prof = _json_obj(profile)
        energy_fields = _resolve_vehicle_energy_fields(defaults, selected, precise_vehicle, vehicle=vehicle, profile=prof)
        params = {
            "energyTypePlat": energy_fields["energy_type_plat"],
            "energyFlag": energy_fields["energy_flag"],
            "clauseType": _field_value(defaults, "条款类型", fallback="F42"),
            "carKindCode": vehicle.get("carKindCode") or "A01",
            "seatCount": _first_text(precise_vehicle.get("vehicleSeat"), selected.get("vehicleSeat"), vehicle.get("seatCount"), "5"),
            "tonCount": _first_text(precise_vehicle.get("tonCount"), selected.get("tonCount"), selected.get("vehicleTonnage"), "0"),
            "enrollDate": vehicle.get("enrollDate") or "",
            "useNatureCode": vehicle.get("useNatureCode") or "211",
            "startDateBI": vehicle.get("startDateBI") or _next_day_text(),
            "purchasePrice": _vehicle_platform_purchase_price(selected, precise_vehicle),
        }
        data = client.request_json(
            "POST",
            CAL_ACTUAL_VALUE_PATH,
            purpose="business",
            params=params,
            headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
        )
        _ensure_platform_success(data, action="车辆实际价值计算")
        return data

    def _platform_next_quote_start_date(self, client: PiccProtocolClient) -> str:
        try:
            data = client.request_json(
                "GET",
                GET_CURRENT_TIME_PATH,
                purpose="business",
                params={"timeFormat": "yyyy-MM-dd"},
                headers={"Referer": f"{client.config.base_url}/khyxui/my-tools/quotation"},
            )
            current_day = _platform_current_day_from_response(data)
            if current_day:
                return _platform_next_quote_start_date_from_day(current_day)
        except Exception:
            pass
        return _next_day_text()

    def _insurance_date_adjustment_from_platform_response(
        self,
        client: PiccProtocolClient,
        platform_response: Any,
        *,
        error_message: Any = "",
        request_body: Any = None,
    ) -> Dict[str, Any]:
        dialog = _used_fuel_quote_platform_dialog(platform_response)
        message = _to_str(dialog.get("message")).strip()
        kinds = [item for item in (dialog.get("adjustment_kinds") if isinstance(dialog.get("adjustment_kinds"), list) else []) if item in {"bi", "ci"}]
        reinsure_items = dialog.get("reinsure_items") if isinstance(dialog.get("reinsure_items"), list) else []
        raw_commercial_candidate = _date_text(dialog.get("raw_suggested_commercial_start_date"))
        raw_compulsory_candidate = _date_text(dialog.get("raw_suggested_compulsory_start_date"))
        raw_commercial_hour = _to_str(dialog.get("raw_suggested_commercial_start_hour")).strip()
        raw_commercial_minute = _to_str(dialog.get("raw_suggested_commercial_start_minute")).strip()
        raw_compulsory_hour = _to_str(dialog.get("raw_suggested_compulsory_start_hour")).strip()
        raw_compulsory_minute = _to_str(dialog.get("raw_suggested_compulsory_start_minute")).strip()
        # Prefer the untouched value parsed from the platform response. The
        # legacy `suggested_*` fields are only a compatibility fallback.
        commercial_candidate = raw_commercial_candidate or _date_text(dialog.get("suggested_commercial_start_date"))
        compulsory_candidate = raw_compulsory_candidate or _date_text(dialog.get("suggested_compulsory_start_date"))
        if not raw_commercial_candidate:
            raw_commercial_candidate = commercial_candidate
        if not raw_compulsory_candidate:
            raw_compulsory_candidate = compulsory_candidate
        adjustment_source = "platform_current_time_and_quote_prompt"

        if not kinds:
            platform_body = _json_obj(_json_obj(platform_response).get("data"))
            platform_text = _join_unique_platform_notice_parts(
                platform_body.get("normalizeErrorMsg"),
                platform_body.get("errorMsg"),
                platform_body.get("errorMessage"),
                _platform_message(platform_response, ""),
            )
            detect_text = _join_unique_platform_notice_parts(platform_text, error_message)
            kinds = _insurance_date_error_adjustment_kinds(detect_text)
            if kinds:
                message = platform_text or detect_text
            else:
                repeat_kinds = _reinsure_notice_adjustment_kinds(detect_text)
                if repeat_kinds:
                    kinds = repeat_kinds
                    message = platform_text or detect_text
                    candidate_day = _reinsure_notice_suggested_start_date(detect_text)
                    candidate_datetime = _reinsure_notice_suggested_start_datetime(detect_text)
                    if "bi" in kinds:
                        commercial_candidate = candidate_day
                        raw_commercial_candidate = candidate_day
                        raw_commercial_hour = _to_str(candidate_datetime.get("hour")).strip()
                        raw_commercial_minute = _to_str(candidate_datetime.get("minute")).strip()
                    if "ci" in kinds:
                        compulsory_candidate = candidate_day
                        raw_compulsory_candidate = candidate_day
                        raw_compulsory_hour = _to_str(candidate_datetime.get("hour")).strip()
                        raw_compulsory_minute = _to_str(candidate_datetime.get("minute")).strip()
                else:
                    implicit_renewal = _implicit_renewal_quote_adjustment_from_response(
                        platform_response,
                        platform_text or detect_text or message,
                    )
                    if implicit_renewal:
                        kinds = [
                            item
                            for item in (
                                implicit_renewal.get("adjustment_kinds")
                                if isinstance(implicit_renewal.get("adjustment_kinds"), list)
                                else []
                            )
                            if _to_str(item).strip() in {"bi", "ci"}
                        ]
                        message = _to_str(implicit_renewal.get("message")).strip() or platform_text or detect_text
                        adjustment_source = _to_str(implicit_renewal.get("source")).strip() or adjustment_source
                        if "bi" in kinds:
                            commercial_candidate = _date_text(implicit_renewal.get("commercial_start_date"))
                            raw_commercial_candidate = commercial_candidate
                            raw_commercial_hour = _to_str(implicit_renewal.get("commercial_start_hour")).strip()
                            raw_commercial_minute = _to_str(implicit_renewal.get("commercial_start_minute")).strip()
                        if "ci" in kinds:
                            compulsory_candidate = _date_text(implicit_renewal.get("compulsory_start_date"))
                            raw_compulsory_candidate = compulsory_candidate
                            raw_compulsory_hour = _to_str(implicit_renewal.get("compulsory_start_hour")).strip()
                            raw_compulsory_minute = _to_str(implicit_renewal.get("compulsory_start_minute")).strip()

        if not kinds:
            return {}

        # A structured reinsure row tells us which coverage was duplicated. An
        # explicit platform error can additionally name a different coverage
        # whose date is invalid. Treat the text as an additive signal so the
        # retry can update commercial and compulsory dates independently.
        explicit_error_kinds = _insurance_date_error_adjustment_kinds(
            _join_unique_platform_notice_parts(message, error_message)
        )
        for kind in explicit_error_kinds:
            if kind not in kinds:
                kinds.append(kind)

        platform_next_day = self._platform_next_quote_start_date(client)
        commercial_start = ""
        compulsory_start = ""
        if "bi" in kinds:
            commercial_start = _platform_effective_quote_date(
                commercial_candidate or platform_next_day,
                min_day=platform_next_day,
            ) or platform_next_day
            if _date_text(commercial_candidate) != commercial_start:
                raw_commercial_hour = ""
                raw_commercial_minute = ""
        if "ci" in kinds:
            compulsory_start = _platform_effective_quote_date(
                compulsory_candidate or platform_next_day,
                min_day=platform_next_day,
            ) or platform_next_day
            if _date_text(compulsory_candidate) != compulsory_start:
                raw_compulsory_hour = ""
                raw_compulsory_minute = ""
        if request_body:
            body_for_compare = _clean_used_fuel_request_body(_json_obj(request_body))
            form_for_compare = _clean_vehicle_cert_fields(_json_obj(body_for_compare.get("quoteForm")))
            vehicle_for_compare = _clean_vehicle_cert_fields(_json_obj(body_for_compare.get("vehicleForm")))
            defaults_for_compare = _json_obj(body_for_compare.get("defaultFields"))
            if (
                "ci" in kinds
                and compulsory_start
                and not _period_time_explicit(raw_compulsory_hour, raw_compulsory_minute)
                and _to_str(form_for_compare.get("renewed")).strip() != "1"
                and adjustment_source == "implicit_renewal_quote_hint"
            ):
                configured_ci_hour = _field_value(defaults_for_compare, "交强起保小时", "交强险起保小时", "转保交强起保小时")
                configured_ci_minute = _field_value(defaults_for_compare, "交强起保分钟", "交强险起保分钟", "转保交强起保分钟")
                if _period_time_explicit(configured_ci_hour, configured_ci_minute):
                    raw_compulsory_hour, raw_compulsory_minute = _period_time_texts(configured_ci_hour, configured_ci_minute)
                else:
                    bi_date = _date_obj(commercial_start)
                    ci_date = _date_obj(compulsory_start)
                    if bi_date and ci_date and (bi_date - ci_date).days == 1:
                        raw_compulsory_hour, raw_compulsory_minute = "14", "0"
            filtered_kinds: List[str] = []
            for kind in kinds:
                target_day = commercial_start if kind == "bi" else compulsory_start
                if _insurance_date_adjustment_needed(
                    form_for_compare,
                    vehicle_for_compare,
                    kind=kind,
                    target_day=target_day,
                    target_hour=raw_commercial_hour if kind == "bi" else raw_compulsory_hour,
                    target_minute=raw_commercial_minute if kind == "bi" else raw_compulsory_minute,
                ):
                    filtered_kinds.append(kind)
            kinds = filtered_kinds
            if "bi" not in kinds:
                commercial_start = ""
                commercial_candidate = ""
                raw_commercial_candidate = ""
                raw_commercial_hour = ""
                raw_commercial_minute = ""
            if "ci" not in kinds:
                compulsory_start = ""
                compulsory_candidate = ""
                raw_compulsory_candidate = ""
                raw_compulsory_hour = ""
                raw_compulsory_minute = ""
            if not kinds:
                return {}
        return {
            "message": message or "平台提示需要修改保险期间，已按平台当前时间自动调整。",
            "start_date": _first_text(commercial_start, compulsory_start),
            "commercial_start_date": commercial_start,
            "compulsory_start_date": compulsory_start,
            "commercial_start_hour": raw_commercial_hour,
            "commercial_start_minute": raw_commercial_minute,
            "compulsory_start_hour": raw_compulsory_hour,
            "compulsory_start_minute": raw_compulsory_minute,
            "raw_commercial_start_date": raw_commercial_candidate,
            "raw_compulsory_start_date": raw_compulsory_candidate,
            "adjustment_kinds": kinds,
            "source": adjustment_source,
            "reinsure_items": reinsure_items[:3] if reinsure_items else [],
        }

    def _apply_insurance_date_adjustment_to_request_body(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        adjustment: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], bool, Dict[str, Any]]:
        body = _clean_used_fuel_request_body(request_body)
        form = _clean_vehicle_cert_fields(_json_obj(body.get("quoteForm")))
        vehicle = _clean_vehicle_cert_fields(_json_obj(body.get("vehicleForm")))
        owner = _clean_vehicle_cert_fields(_json_obj(body.get("ownerForm")))
        defaults = dict(_json_obj(body.get("defaultFields")))
        preflight = dict(_json_obj(body.get("preflight")))
        selected = _json_obj(preflight.get("selectedVehicle"))
        precise_vehicle = _json_obj(preflight.get("preciseVehicle"))
        profile = _motor_quote_profile(body.get("accountTypeName")) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        if not form or not vehicle:
            return body, False, {}

        kinds = [item for item in (adjustment.get("adjustment_kinds") if isinstance(adjustment.get("adjustment_kinds"), list) else []) if item in {"bi", "ci"}]
        bi_day = _date_text(adjustment.get("commercial_start_date"))
        ci_day = _date_text(adjustment.get("compulsory_start_date"))
        bi_hour, bi_minute = _period_time_texts(
            adjustment.get("commercial_start_hour"),
            adjustment.get("commercial_start_minute"),
        )
        ci_hour, ci_minute = _period_time_texts(
            adjustment.get("compulsory_start_hour"),
            adjustment.get("compulsory_start_minute"),
        )
        if (
            "ci" in kinds
            and ci_day
            and not _period_time_explicit(adjustment.get("compulsory_start_hour"), adjustment.get("compulsory_start_minute"))
            and _to_str(form.get("renewed")).strip() != "1"
            and _to_str(adjustment.get("source")).strip() == "implicit_renewal_quote_hint"
        ):
            configured_ci_hour = _field_value(defaults, "交强起保小时", "交强险起保小时", "转保交强起保小时")
            configured_ci_minute = _field_value(defaults, "交强起保分钟", "交强险起保分钟", "转保交强起保分钟")
            if _period_time_explicit(configured_ci_hour, configured_ci_minute):
                ci_hour, ci_minute = _period_time_texts(configured_ci_hour, configured_ci_minute)
            else:
                bi_date = _date_obj(bi_day)
                ci_date = _date_obj(ci_day)
                # 0817 手工报价显示：普通 quote.do 返回“符合续保条件”且商业/交强到期差一天时，
                # 页面会把交强按 14:00-14:00 提交；显式 quotePolicy 续保仍保持平台回填时间。
                if bi_date and ci_date and (bi_date - ci_date).days == 1:
                    ci_hour, ci_minute = "14", "0"
        applied_bi_day = ""
        applied_ci_day = ""
        bi_changed = False
        changed = False

        if (
            "bi" in kinds
            and bi_day
            and _insurance_date_adjustment_needed(
                form,
                vehicle,
                kind="bi",
                target_day=bi_day,
                target_hour=bi_hour,
                target_minute=bi_minute,
            )
        ):
            apply_bi_day = _insurance_date_adjustment_target_day(
                form,
                vehicle,
                kind="bi",
                target_day=bi_day,
            )
        else:
            apply_bi_day = ""
        if apply_bi_day:
            applied_bi_day = apply_bi_day
            if _to_str(form.get("prpCmain.startDate")).strip() != apply_bi_day:
                form["prpCmain.startDate"] = apply_bi_day
                changed = True
                bi_changed = True
            if _to_str(vehicle.get("startDateBI")).strip() != apply_bi_day:
                vehicle["startDateBI"] = apply_bi_day
                changed = True
                bi_changed = True
            if _safe_int_local(form.get("prpCmain.starthourbi"), 0) != _safe_int_local(bi_hour, 0):
                form["prpCmain.starthourbi"] = bi_hour
                changed = True
            if _safe_int_local(form.get("prpCmain.startminutebi"), 0) != _safe_int_local(bi_minute, 0):
                form["prpCmain.startminutebi"] = bi_minute
                changed = True
        if (
            "ci" in kinds
            and ci_day
            and _insurance_date_adjustment_needed(
                form,
                vehicle,
                kind="ci",
                target_day=ci_day,
                target_hour=ci_hour,
                target_minute=ci_minute,
            )
        ):
            apply_ci_day = _insurance_date_adjustment_target_day(
                form,
                vehicle,
                kind="ci",
                target_day=ci_day,
            )
        else:
            apply_ci_day = ""
        if apply_ci_day:
            applied_ci_day = apply_ci_day
            if _to_str(form.get("prpCmain.startDateCI")).strip() != apply_ci_day:
                form["prpCmain.startDateCI"] = apply_ci_day
                changed = True
            if _to_str(vehicle.get("startDateCI")).strip() != apply_ci_day:
                vehicle["startDateCI"] = apply_ci_day
                changed = True
            end_date_ci = _ci_end_date_text(apply_ci_day, ci_hour, ci_minute)
            if end_date_ci and _to_str(form.get("prpCmain.endDateCI")).strip() != end_date_ci:
                form["prpCmain.endDateCI"] = end_date_ci
                changed = True
            expected_end_hour = "24" if ci_hour == "0" and ci_minute == "0" else ci_hour
            if _safe_int_local(form.get("prpCmain.starthourci"), 0) != _safe_int_local(ci_hour, 0):
                form["prpCmain.starthourci"] = ci_hour
                changed = True
            if _safe_int_local(form.get("prpCmain.startminuteci"), 0) != _safe_int_local(ci_minute, 0):
                form["prpCmain.startminuteci"] = ci_minute
                changed = True
            if _safe_int_local(form.get("prpCmain.endhourci"), 24) != _safe_int_local(expected_end_hour, 24):
                form["prpCmain.endhourci"] = expected_end_hour
                changed = True
            if _safe_int_local(form.get("prpCmain.endminuteci"), 0) != _safe_int_local(ci_minute, 0):
                form["prpCmain.endminuteci"] = ci_minute
                changed = True

        recalculated_actual_value = ""
        if bi_changed and selected:
            try:
                actual_value_result = self._query_actual_value(client, vehicle, defaults, selected, precise_vehicle, profile=profile)
                platform_purchase_price = _vehicle_platform_purchase_price(selected, precise_vehicle)
                actual_value = _actual_value_from_response(actual_value_result, platform_purchase_price or selected.get("actualValue"))
                if _profile_text(profile, "new_car_flag") and platform_purchase_price:
                    actual_value = platform_purchase_price
                recalculated_actual_value = _money_text(actual_value)
                if recalculated_actual_value:
                    vehicle["actualValue"] = recalculated_actual_value
                    form["prpCitemCar.actualValue"] = recalculated_actual_value
                    form["prpCitemCar.referenceActualValue"] = recalculated_actual_value

                    vehicle_search = _json_obj(preflight.get("vehicleSearch"))
                    has_explicit_loss = (
                        _money(vehicle_search.get("requestedLossAmount")) > 0
                        or _has_text(_default_value(defaults, PRODUCT_LOSS))
                        or _product_excluded(defaults, PRODUCT_LOSS)
                    )
                    if not has_explicit_loss:
                        loss_index = _quote_form_kind_index(form, "051050")
                        if loss_index is not None:
                            form[f"prpCitemKindVos[{loss_index}].amount"] = recalculated_actual_value
                        product_form = dict(_json_obj(body.get("productForm")))
                        products = product_form.get("products") if isinstance(product_form.get("products"), list) else []
                        for row in products:
                            if isinstance(row, dict) and _canonical_product_name(row.get("name")) == _canonical_product_name(PRODUCT_LOSS):
                                row["insuredAmount"] = recalculated_actual_value
                        if product_form:
                            body["productForm"] = product_form
            except Exception as exc:
                preflight["insuranceDateAutoAdjustActualValueError"] = str(exc)[:300] or exc.__class__.__name__

        if kinds and _normalize_platform_adjusted_quote_products(form, defaults, profile, adjustment):
            changed = True

        if not changed:
            return body, False, {}

        applied_kinds = [
            kind
            for kind, day in (("bi", applied_bi_day), ("ci", applied_ci_day))
            if day
        ]
        notice = {
            "type": "insurance_date_adjust",
            "message": _to_str(adjustment.get("message")).strip(),
            "commercial_start_date": applied_bi_day,
            "compulsory_start_date": applied_ci_day,
            "commercial_start_hour": bi_hour if applied_bi_day else "",
            "commercial_start_minute": bi_minute if applied_bi_day else "",
            "compulsory_start_hour": ci_hour if applied_ci_day else "",
            "compulsory_start_minute": ci_minute if applied_ci_day else "",
            "adjustment_kinds": applied_kinds,
            "actual_value": recalculated_actual_value,
            "source": adjustment.get("source") or "platform_prompt",
        }
        preflight["insuranceDateAutoAdjusted"] = notice
        body["quoteForm"] = _clean_vehicle_cert_fields(form)
        body["vehicleForm"] = _clean_vehicle_cert_fields(vehicle)
        body["ownerForm"] = _clean_vehicle_cert_fields(owner)
        body["defaultFields"] = defaults
        body["preflight"] = preflight
        return _clean_used_fuel_request_body(body), True, notice

    def _submit_used_fuel_quote_with_vehicle_retry(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        body = _clean_used_fuel_request_body(request_body)
        try:
            return body, self._submit_used_fuel_quote(client, body)
        except PiccBusinessRequestError as exc:
            platform_text = json.dumps(getattr(exc, "platform_response", None), ensure_ascii=False, default=str)
            if not (_vehicle_platform_mismatch_message(str(exc)) or _vehicle_platform_mismatch_message(platform_text)):
                raise
            platform_codes = _vehicle_platform_mismatch_codes(str(exc)) + _vehicle_platform_mismatch_codes(platform_text)
            corrected_body, corrected = self._rebuild_quote_for_platform_vehicle_code(client, body, platform_codes)
            if not corrected:
                corrected_body, corrected = _accept_platform_returned_vehicle_body(body)
            if not corrected:
                raise
            return corrected_body, self._submit_used_fuel_quote(client, corrected_body)

    def _submit_used_fuel_quote_with_period_auto_adjust(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        *,
        auto_notice_callback: Any = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        notices: List[Dict[str, Any]] = []
        emitted_notice_signatures: set[tuple[str, str, str, str]] = set()
        emitted_notice_texts: List[str] = []
        body = _clean_used_fuel_request_body(request_body)

        def remember_notice(adjustment: Mapping[str, Any], notice: Mapping[str, Any]) -> None:
            item = dict(_json_obj(notice))
            message_text = _to_str(adjustment.get("message")).strip()
            message_compact = re.sub(r"\s+", "", message_text)
            signature = (
                message_text,
                _date_text(notice.get("commercial_start_date")),
                _date_text(notice.get("compulsory_start_date")),
                ",".join(_to_str(x).strip() for x in (notice.get("adjustment_kinds") if isinstance(notice.get("adjustment_kinds"), list) else [])),
            )
            emitted = False
            contained_by_emitted_notice = bool(
                message_compact
                and any(message_compact in previous for previous in emitted_notice_texts)
            )
            if signature not in emitted_notice_signatures and not contained_by_emitted_notice:
                emitted_notice_signatures.add(signature)
                emitted_notice_texts.append(message_compact)
                emitted = _emit_insurance_date_adjust_notice(auto_notice_callback, adjustment)
            if emitted:
                item["emitted_to_chat"] = True
            notices.append(item)

        def remember_platform_notice(notice: Mapping[str, Any]) -> None:
            item = dict(_json_obj(notice))
            message_text = _to_str(item.get("message")).strip()
            message_compact = re.sub(r"\s+", "", message_text)
            if not message_compact:
                return
            if any(message_compact in previous or previous in message_compact for previous in emitted_notice_texts):
                item["emitted_to_chat"] = True
                notices.append(item)
                return
            emitted_notice_texts.append(message_compact)
            emitted = _emit_platform_auto_notice(auto_notice_callback, item)
            if emitted:
                item["emitted_to_chat"] = True
            notices.append(item)

        # HAR confirms the page may report commercial and compulsory date issues
        # one after another. Treat both exception responses and successful quote
        # responses as state-machine transitions, and keep the loop bounded.
        last_error: Optional[PiccBusinessRequestError] = None
        for _ in range(6):
            try:
                body, quote_response = self._submit_used_fuel_quote_with_vehicle_retry(client, body)
            except PiccBusinessRequestError as exc:
                last_error = exc
                adjustment = self._insurance_date_adjustment_from_platform_response(
                    client,
                    getattr(exc, "platform_response", None),
                    error_message=str(exc),
                    request_body=body,
                )
                adjusted_body, changed, notice = self._apply_insurance_date_adjustment_to_request_body(client, body, adjustment)
                if not changed:
                    exc.platform_auto_notices = [
                        *getattr(exc, "platform_auto_notices", []),
                        *notices,
                    ]
                    raise
                remember_notice(adjustment, notice)
                body = adjusted_body
                continue

            adjustment = self._insurance_date_adjustment_from_platform_response(client, quote_response, request_body=body)
            adjusted_body, changed, notice = self._apply_insurance_date_adjustment_to_request_body(client, body, adjustment)
            if not changed:
                if not _quote_response_has_display_result(quote_response):
                    platform_dialog = _used_fuel_quote_platform_dialog(quote_response)
                    if platform_dialog and _to_str(platform_dialog.get("subtype")).strip().lower() != "insurance_date_adjust":
                        platform_notice = _platform_notice_auto_notice_from_dialog(platform_dialog)
                        if platform_notice:
                            remember_platform_notice(platform_notice)
                            return body, quote_response, notices
                return body, quote_response, notices
            remember_notice(adjustment, notice)
            body = adjusted_body

        if last_error is not None:
            last_error.platform_auto_notices = [
                *getattr(last_error, "platform_auto_notices", []),
                *notices,
            ]
            raise last_error
        raise PiccRequestError("平台连续提示修改保险期间，已达到自动调整上限，请人工核对起保日期后重试")

    def _used_fuel_products(
        self,
        defaults: Mapping[str, Any],
        *,
        profile: Optional[Mapping[str, Any]] = None,
        actual_value: Any,
        seat_count: Any,
    ) -> List[Dict[str, Any]]:
        prof = _json_obj(profile)
        seats = max(1, int(_money(seat_count, "5")))
        driver_default = _profile_product_default(defaults, prof, PRODUCT_DRIVER, "2")
        passenger_default = _profile_product_default(defaults, prof, PRODUCT_PASSENGER, "2")
        driver_amount = _wan_or_amount_to_amount(driver_default, "2")
        passenger_per_seat = Decimal(_wan_or_amount_to_amount(passenger_default, "2"))
        passenger_amount = str(int((passenger_per_seat * Decimal(max(seats - 1, 1))).quantize(Decimal("1"))))
        loss_amount = _first_text(_profile_product_default(defaults, prof, PRODUCT_LOSS), actual_value, "0")
        third_party_default = _profile_product_default(defaults, prof, PRODUCT_THIRD_PARTY, "300")
        third_party_amount = _wan_or_amount_to_amount(third_party_default, "300")
        shared_main_limit = _checked(_profile_product_default(defaults, prof, PRODUCT_SHARED_LIMIT, True), default=True)
        medical_third_amount = _wan_or_amount_to_amount(
            _profile_product_default(defaults, prof, PRODUCT_MEDICAL_THIRD, third_party_default),
            _wan_or_amount_to_wan_text(third_party_default, "300"),
        )
        if shared_main_limit:
            medical_third_amount = third_party_amount
        road_rescue_quantity = _safe_int_local(_default_value(defaults, PRODUCT_ROAD_RESCUE, ""), 0)
        external_grid_amount = _external_grid_amount(defaults, actual_value)
        rows = [
            {"code": "CI", "name": PRODUCT_COMPULSORY, "required": True, "coverage": _to_str(_profile_product_default(defaults, prof, PRODUCT_COMPULSORY, "20"))},
            {"code": "BI050", "name": PRODUCT_LOSS, "required": True, "insuredAmount": _money_text(loss_amount)},
            {"code": "BI051", "name": PRODUCT_THIRD_PARTY, "required": True, "insuredAmount": third_party_amount},
            {"code": "BI060", "name": PRODUCT_DRIVER, "required": True, "insuredAmount": driver_amount},
            {"code": "BI061", "name": PRODUCT_PASSENGER, "required": True, "insuredAmount": passenger_amount},
            {"code": "BI_SHARED", "name": PRODUCT_SHARED_LIMIT, "required": True, "checked": shared_main_limit},
            {"code": "BI_MEDICAL_THIRD", "name": PRODUCT_MEDICAL_THIRD, "required": True, "insuredAmount": medical_third_amount},
        ]
        if road_rescue_quantity > 0:
            rows.append({"code": "BI_ROAD_RESCUE", "name": PRODUCT_ROAD_RESCUE, "required": False, "quantity": road_rescue_quantity})
        if _is_positive_amount(external_grid_amount):
            rows.append({"code": "BI_EXTERNAL_GRID", "name": PRODUCT_EXTERNAL_GRID, "required": False, "insuredAmount": external_grid_amount})
        exclusions = _product_exclusions(defaults)
        if exclusions:
            rows = [row for row in rows if _canonical_product_name(row.get("name")) not in exclusions]
        return rows

    def _build_motor_quote_result_from_response(
        self,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        request_body: Mapping[str, Any],
        quote_response: Mapping[str, Any],
    ) -> Dict[str, Any]:
        account_type_name = _normalize_account_type(
            request_body.get("accountTypeName")
            or ctx.account_type_name
            or _json_obj(quote_payload).get("account_type_name")
            or USED_FUEL_ACCOUNT_TYPE
        )
        profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        is_new_energy_vehicle = picc_is_new_energy_vehicle(
            account_type_name=account_type_name,
            is_energy_car=_profile_text(profile, "is_energy_car"),
        )
        data = _clean_vehicle_cert_fields(_json_obj(_json_obj(quote_response).get("data")))
        vehicle = _clean_vehicle_cert_fields(_json_obj(request_body.get("vehicleForm")))
        owner = _clean_vehicle_cert_fields(_json_obj(request_body.get("ownerForm")))
        form = _clean_vehicle_cert_fields(_json_obj(request_body.get("quoteForm")))
        shared_main_limit = _quote_form_shared_main_limit(form)
        preflight = _json_obj(request_body.get("preflight"))
        selected_vehicle = _json_obj(preflight.get("selectedVehicle"))
        precise_vehicle = _json_obj(preflight.get("preciseVehicle"))
        item_rows = _json_obj(quote_response).get("itemKindTempList")
        item_rows_source = "quote_response.itemKindTempList"
        if not isinstance(item_rows, list):
            item_rows = data.get("itemKindTempList")
            item_rows_source = "quote_response.data.itemKindTempList"
        if not isinstance(item_rows, list):
            item_rows = []

        coverage_items: List[Dict[str, Any]] = []
        core_premium_evidence: List[Dict[str, Any]] = []
        joint_sales_evidence: List[Dict[str, Any]] = []
        commercial_premium_from_rows = Decimal("0")
        commercial_premium_rows_present = False
        commercial_primary_rows_present = False
        compulsory_premium_value: Optional[Decimal] = _money(data.get("ciPremium")) if _has_text(data.get("ciPremium")) else None
        commercial_premium_source = ""
        compulsory_premium_source = ""
        vehicle_tax_source = ""
        total_without_vehicle_tax_source = ""
        total_with_vehicle_tax_source = ""
        if _has_text(data.get("biPremium")):
            commercial_premium_source = "quote_response.data.biPremium"
            core_premium_evidence.append(
                {
                    "name": "commercial",
                    "source": "quote_response.data.biPremium",
                    "value": _clean_money_text(data.get("biPremium")),
                }
            )
        if _has_text(data.get("ciPremium")):
            compulsory_premium_source = "quote_response.data.ciPremium"
            core_premium_evidence.append(
                {
                    "name": "compulsory",
                    "source": "quote_response.data.ciPremium",
                    "value": _clean_money_text(data.get("ciPremium")),
                }
            )
        for row_index, row_any in enumerate(item_rows):
            row = _json_obj(row_any)
            kind_code = _to_str(row.get("kindCode")).strip()
            platform_kind_name = _to_str(row.get("kindName")).strip()
            name = picc_result_kind_name(
                kind_code,
                platform_name=platform_kind_name,
                is_new_energy=is_new_energy_vehicle,
            )
            premium_present = _has_text(row.get("premium"))
            premium = _money(row.get("premium")) if premium_present else Decimal("0")
            if kind_code == "051074" or name == "交强险":
                if compulsory_premium_value is None and premium_present:
                    compulsory_premium = premium
                    compulsory_premium_value = premium
                    compulsory_premium_source = f"{item_rows_source}[{row_index}].premium"
                    core_premium_evidence.append(
                        {
                            "name": "compulsory",
                            "source": f"{item_rows_source}[{row_index}].premium",
                            "value": _clean_money_text(row.get("premium")),
                        }
                    )
                continue
            if premium_present:
                commercial_premium_from_rows += premium
                commercial_premium_rows_present = True
                if kind_code in PICC_CORE_MOTOR_KIND_CODES:
                    commercial_primary_rows_present = True
                    core_premium_evidence.append(
                        {
                            "name": "commercial",
                            "source": f"{item_rows_source}[{row_index}].premium",
                            "value": _clean_money_text(row.get("premium")),
                        }
                    )
            coverage_items.append(
                {
                    "code": kind_code,
                    "name": name,
                    "platform_name": platform_kind_name,
                    "amount": _clean_money_text_or_empty(row.get("amount")),
                    "amount_text": picc_result_amount_text(
                        row,
                        seat_count=_first_text(vehicle.get("seatCount"), form.get("prpCitemCar.seatCount")),
                        shared_main_limit=shared_main_limit,
                    ),
                    "unit_amount": _clean_money_text_or_empty(row.get("unitAmount")),
                    "quantity": _to_str(row.get("quantity")).strip(),
                    "shared_amount_flag": _to_str(row.get("sharedAmountFlag")).strip(),
                    "premium": _money_text_or_empty(row.get("premium")),
                    "premium_text": _proposal_money_yuan(row.get("premium")),
                }
            )

        commercial_premium_value: Optional[Decimal] = (
            _money(data.get("biPremium"))
            if _has_text(data.get("biPremium"))
            # The platform sometimes omits biPremium, in which case its
            # motor rows are a valid real source. An add-on alone must not be
            # promoted into a commercial quote.
            else (
                commercial_premium_from_rows
                if commercial_premium_rows_present and commercial_primary_rows_present
                else None
            )
        )
        if commercial_premium_value is not None and not commercial_premium_source:
            commercial_premium_source = f"{item_rows_source}[*].premium.sum"
        vehicle_tax_raw = _first_text(data.get("sumPayTax"), data.get("thisPayTax"), data.get("carShipTaxes"))
        vehicle_tax_value: Optional[Decimal] = _money(vehicle_tax_raw) if _has_text(vehicle_tax_raw) else None
        for tax_key in ("sumPayTax", "thisPayTax", "carShipTaxes"):
            if _has_text(data.get(tax_key)):
                vehicle_tax_source = f"quote_response.data.{tax_key}"
                break
        if vehicle_tax_value is None and (_has_text(data.get("prePayTax")) or _has_text(data.get("delayPayTax"))):
            vehicle_tax_value = _money(data.get("prePayTax")) + _money(data.get("delayPayTax"))
            vehicle_tax_source = "derived_from_quote_response.data.prePayTax+delayPayTax"

        risk_score: Any = ""
        picc_score = _to_str(data.get("piccScore")).strip()
        if picc_score:
            try:
                parsed_score = int(picc_score)
            except (TypeError, ValueError):
                parsed_score = None
            risk_score = parsed_score if parsed_score is not None else ""
        warning_parts = []
        quote_warning = _strip_platform_error_code(data.get("errorMessage"))
        if quote_warning:
            warning_parts.append(quote_warning)
        duplicate_warning = _to_str(
            _json_obj(
                _json_obj(
                    _json_obj(request_body.get("preflight")).get("quotePrechecks")
                ).get("duplicateVin")
            ).get("warning")
        ).strip()
        if duplicate_warning:
            warning_parts.append(duplicate_warning)
        tujia_anshun = _tujia_anshun_from_request_body(request_body)
        if tujia_anshun.get("enabled") and tujia_anshun.get("success") is False:
            msg = _to_str(tujia_anshun.get("message")).strip()
            warning_parts.append(f"途家安顺保额查询失败：{msg or '平台未返回可用方案'}")
        warning = "\n".join(part for part in warning_parts if part)

        bi_risk = _json_obj(data.get("carQuoteRiskItemBIRsp"))
        ci_risk = _json_obj(data.get("carQuoteRiskItemCIRsp"))
        claim_bi_raw = _first_text(bi_risk.get("claimTimes"), data.get("lastDamagedBI"))
        claim_ci_raw = _first_text(ci_risk.get("claimTimes"), data.get("lastDamagedCI"))
        claim_bi = _safe_int_local(claim_bi_raw, 0) if _has_text(claim_bi_raw) else ""
        claim_ci = _safe_int_local(claim_ci_raw, 0) if _has_text(claim_ci_raw) else ""
        platform_joint_sales_premium_raw_present = _has_text(data.get("sumYelPremium"))
        platform_joint_sales_premium = _money(data.get("sumYelPremium")) if platform_joint_sales_premium_raw_present else Decimal("0")
        platform_joint_sales_premium_present = platform_joint_sales_premium_raw_present and platform_joint_sales_premium > 0
        if platform_joint_sales_premium_present:
            joint_sales_evidence.append(
                {
                    "name": "joint_sales",
                    "source": "quote_response.data.sumYelPremium",
                    "value": _clean_money_text(data.get("sumYelPremium")),
                }
            )
        joint_sales_premium_present = platform_joint_sales_premium_present
        joint_sales_premium = platform_joint_sales_premium
        tujia_premium = _money(tujia_anshun.get("premium"))
        joint_sales_premium_from_plan = False
        selected_plan = _json_obj(tujia_anshun.get("selected_plan"))
        selected_plan_premium = _money(selected_plan.get("planPremium"))
        # A configured premium is only an input to the plan lookup. It is not
        # a quote result unless that lookup actually returned a usable plan.
        tujia_plan_success = tujia_anshun.get("success") is True
        if (
            tujia_plan_success
            and selected_plan_premium > 0
            and (not joint_sales_premium_present or joint_sales_premium <= 0)
        ):
            joint_sales_premium = selected_plan_premium
            joint_sales_premium_present = True
            joint_sales_premium_from_plan = True
            joint_sales_evidence.append(
                {
                    "name": "joint_sales",
                    "source": "joint_sales_plan_response.selected_plan.planPremium",
                    "value": _clean_money_text(selected_plan_premium),
                }
            )
        elif tujia_plan_success and tujia_premium > 0 and selected_plan_premium <= 0:
            warning_parts.append("途家安顺保额查询返回方案缺少真实保费，未填充配置保费")
        joint_sales_amount = (
            _money(
                selected_plan.get("planAmount")
                if _has_text(selected_plan.get("planAmount"))
                else tujia_anshun.get("amount")
            )
            if tujia_plan_success
            and (
                _has_text(selected_plan.get("planAmount"))
                or _has_text(tujia_anshun.get("amount"))
            )
            else Decimal("0")
        )
        joint_sales_amount_present = joint_sales_premium > 0 and joint_sales_amount > 0
        if not joint_sales_premium_present:
            joint_sales_amount_present = False
            joint_sales_amount = Decimal("0")
        if platform_joint_sales_premium_present:
            joint_sales_source = "platform_quote_response"
        elif joint_sales_premium_from_plan:
            joint_sales_source = "joint_sales_plan_response"
        else:
            joint_sales_source = "none"
        if _has_text(data.get("sumPremium")):
            total_without_vehicle_tax: Optional[Decimal] = _money(data.get("sumPremium"))
            total_without_vehicle_tax_source = "quote_response.data.sumPremium"
            if joint_sales_premium_from_plan and platform_joint_sales_premium <= 0:
                total_without_vehicle_tax += joint_sales_premium
                total_without_vehicle_tax_source = (
                    "derived_from_quote_response.data.sumPremium+"
                    "joint_sales_plan_response.selected_plan.planPremium"
                )
        elif commercial_premium_value is not None and compulsory_premium_value is not None:
            total_without_vehicle_tax = commercial_premium_value + compulsory_premium_value + (joint_sales_premium if joint_sales_premium_present else Decimal("0"))
            total_without_vehicle_tax_source = "derived_from_quote_response_components"
            if joint_sales_premium_from_plan:
                total_without_vehicle_tax_source += "+joint_sales_plan_response.selected_plan.planPremium"
        else:
            total_without_vehicle_tax = None

        if _has_text(data.get("totalPremium") or data.get("premiumTotal")):
            total_with_vehicle_tax: Optional[Decimal] = _money(data.get("totalPremium") or data.get("premiumTotal"))
            total_with_vehicle_tax_source = (
                "quote_response.data.totalPremium"
                if _has_text(data.get("totalPremium"))
                else "quote_response.data.premiumTotal"
            )
        elif total_without_vehicle_tax is not None and vehicle_tax_value is not None:
            total_with_vehicle_tax = total_without_vehicle_tax + vehicle_tax_value
            total_with_vehicle_tax_source = "derived_from_quote_response_components"
        else:
            # Never label an unknown tax-inclusive total with the pre-tax
            # amount. A missing platform total/tax is unknown, not zero.
            total_with_vehicle_tax = None
        # Keep the historical field as the final payable total while exposing both table totals explicitly.
        total = total_with_vehicle_tax
        vehicle_type_code = _first_text(data.get("carKindCode"), data.get("vehicleClassPicc"), form.get("prpCitemCar.carKindCode"), vehicle.get("carKindCode"))
        use_nature_code = _first_text(data.get("vehicleUseNatureCode"), form.get("prpCitemCar.useNatureCode"), vehicle.get("useNatureCode"))
        ton_value = _first_text(
            data.get("tonCount"),
            data.get("vehicleTonnage"),
            form.get("prpCitemCar.tonCount"),
            vehicle.get("tonCount"),
            precise_vehicle.get("tonCount"),
            selected_vehicle.get("tonCount"),
            selected_vehicle.get("vehicleTonnage")
        )
        seat_value = _first_text(data.get("seatCount"), data.get("vehicleSeat"), form.get("prpCitemCar.seatCount"), vehicle.get("seatCount"), selected_vehicle.get("vehicleSeat"))
        proposal_info = {
            "insured_name": _first_text(data.get("insueredName"), data.get("insuredName"), owner.get("ownerName"), form.get("carOwner")),
            "plate_no": _first_text(data.get("licenseNo"), form.get("prpCitemCar.licenseNo"), vehicle.get("licenseNo")),
            "engine_no": _first_text(data.get("engineNo"), form.get("prpCitemCar.engineNo"), vehicle.get("engineNo")),
            "vin": _first_text(data.get("vinNo"), data.get("frameNo"), form.get("prpCitemCar.vinNo"), vehicle.get("vin")),
            "vehicle_type": _code_label(vehicle_type_code, PICC_CAR_KIND_LABELS),
            "vehicle_usage": _code_label(use_nature_code, PICC_USE_NATURE_LABELS),
            "vehicle_model": _first_text(
                data.get("vehicleName"),
                data.get("brandName"),
                form.get("prpCitemCar.brandName"),
                selected_vehicle.get("vehicleName"),
                vehicle.get("selectedModelName"),
                precise_vehicle.get("vehicleName"),
                vehicle.get("modelName"),
            ),
            "enroll_date": _first_text(_date_text(form.get("prpCitemCar.enrollDate")), _date_text(vehicle.get("enrollDate"))),
            "ton_count": f"{_clean_money_text(ton_value)}千克" if _has_text(ton_value) else "",
            "seat_count": f"{_safe_int_local(seat_value, 0)}人" if _has_text(seat_value) else "",
            "purchase_price": _proposal_money_yuan(
                _first_text(form.get("prpCitemCar.purchasePrice"), vehicle.get("purchasePrice"), selected_vehicle.get("purchasePrice"), selected_vehicle.get("priceP"), selected_vehicle.get("priceT")),
                keep_decimal=False,
            ),
            "claim_summary": _proposal_claim_summary(data, claim_bi, claim_ci),
            "bi_start_date": _proposal_start_datetime_from_quote_response(data, form, kind="bi"),
            "ci_start_date": _proposal_start_datetime_from_quote_response(data, form, kind="ci"),
            "model_match_method": _first_text(
                vehicle.get("modelQueryMatchLabel"),
                vehicle.get("modelQueryMatched"),
            ),
            "model_match_kind": _to_str(vehicle.get("modelQueryMatchKind")).strip(),
            "model_query_matched": _to_str(vehicle.get("modelQueryMatched")).strip(),
        }
        result_card = {
            "style": "picc_proposal_table",
            "title": "中国人保投保方案",
            "include_tax": True,
            "total_premium": _money_text(total_with_vehicle_tax) if total_with_vehicle_tax is not None else "",
            "total_without_vehicle_tax": _money_text(total_without_vehicle_tax) if total_without_vehicle_tax is not None else "",
            "total_with_vehicle_tax": _money_text(total_with_vehicle_tax) if total_with_vehicle_tax is not None else "",
            "commercial_premium": _money_text(commercial_premium_value) if commercial_premium_value is not None else "",
            "compulsory_premium": _money_text(compulsory_premium_value) if compulsory_premium_value is not None else "",
            "vehicle_tax": _money_text(vehicle_tax_value) if vehicle_tax_value is not None else "",
            "vehicle_tax_detail": {
                "current": _money_text_or_empty(data.get("thisPayTax")),
                "back": _money_text_or_empty(data.get("prePayTax")),
                "late_fee": _money_text_or_empty(data.get("delayPayTax")),
            },
            "joint_sales_label": "途家安顺",
            "joint_sales_display_label": "途顺家安组合保险",
            "joint_sales_premium": _money_text(joint_sales_premium) if joint_sales_premium_present else "",
            "joint_sales_amount": _money_text(joint_sales_amount) if joint_sales_amount_present else "",
            "joint_sales_plan_name": _to_str(_json_obj(tujia_anshun.get("selected_plan")).get("planName")).strip(),
            "joint_sales_plan_code": _to_str(_json_obj(tujia_anshun.get("selected_plan")).get("planCode")).strip(),
            "driver_accident_premium": _money_text_or_empty(data.get("DDAPremium")),
            "claim_business_count": claim_bi,
            "claim_compulsory_count": claim_ci,
            "risk_score": risk_score,
            "vehicle_energy_type": "new_energy" if is_new_energy_vehicle else "fuel",
            "coverage_items": coverage_items,
            "proposal_info": proposal_info,
            "proposal_coverage_items": [dict(item) for item in coverage_items],
            "transfer_available": True,
        }
        price_items = []
        if commercial_premium_value is not None:
            price_items.append({"name": "商业险", "amount": float(commercial_premium_value)})
        if compulsory_premium_value is not None:
            price_items.append({"name": "交强险", "amount": float(compulsory_premium_value)})
        if vehicle_tax_value is not None:
            price_items.append({"name": "车船税", "amount": float(vehicle_tax_value)})
        if joint_sales_premium:
            price_items.append({"name": "途家安顺", "amount": float(joint_sales_premium)})
        normalized_amounts: Dict[str, Dict[str, Any]] = {}
        if commercial_premium_value is not None:
            normalized_amounts["commercial"] = {
                "value": _clean_money_text(commercial_premium_value),
                "source": commercial_premium_source,
            }
        if compulsory_premium_value is not None:
            normalized_amounts["compulsory"] = {
                "value": _clean_money_text(compulsory_premium_value),
                "source": compulsory_premium_source,
            }
        if vehicle_tax_value is not None:
            normalized_amounts["vehicle_tax"] = {
                "value": _clean_money_text(vehicle_tax_value),
                "source": vehicle_tax_source,
            }
        if joint_sales_premium_present:
            normalized_amounts["joint_sales"] = {
                "value": _clean_money_text(joint_sales_premium),
                "source": (
                    "quote_response.data.sumYelPremium"
                    if platform_joint_sales_premium_present
                    else "joint_sales_plan_response.selected_plan.planPremium"
                ),
            }
        if total_without_vehicle_tax is not None:
            normalized_amounts["total_without_vehicle_tax"] = {
                "value": _clean_money_text(total_without_vehicle_tax),
                "source": total_without_vehicle_tax_source,
            }
        if total_with_vehicle_tax is not None:
            normalized_amounts["total_with_vehicle_tax"] = {
                "value": _clean_money_text(total_with_vehicle_tax),
                "source": total_with_vehicle_tax_source,
            }
        return {
            "mode": _profile_text(profile, "mode", "picc_motor_real"),
            "status": "quoted",
            "platform_code": "PICC",
            "platform_name": "人保",
            "quote_provenance": {
                "source": "platform_quote_response",
                "platform_code": "PICC",
                "response_status": _json_obj(quote_response).get("status"),
                "quotation_no": data.get("quotationNo"),
                "quotation_id": data.get("quotationId"),
                "core_premium_evidence": core_premium_evidence,
                "joint_sales_evidence": joint_sales_evidence,
                "normalized_amounts": normalized_amounts,
            },
            "account_type_name": account_type_name,
            "vehicle_energy_type": "new_energy" if is_new_energy_vehicle else "fuel",
            "quotation_no": data.get("quotationNo"),
            "quotation_id": data.get("quotationId"),
            "plate_no": _first_text(data.get("licenseNo"), vehicle.get("licenseNo")),
            "owner_name": owner.get("ownerName"),
            "vehicle_model": vehicle.get("selectedModelName") or vehicle.get("modelName"),
            "vehicle_actual_value": _money_text_or_empty(vehicle.get("actualValue")),
            "joint_sales": tujia_anshun,
            "joint_sales_source": joint_sales_source,
            "joint_sales_premium": _money_text(joint_sales_premium) if joint_sales_premium_present else "",
            "joint_sales_amount": _money_text(joint_sales_amount) if joint_sales_amount_present else "",
            "driver_accident_premium": _money_text_or_empty(data.get("DDAPremium")),
            "claim_business_count": claim_bi,
            "claim_compulsory_count": claim_ci,
            "vehicle_tax_detail": {
                "current": _money_text_or_empty(data.get("thisPayTax")),
                "back": _money_text_or_empty(data.get("prePayTax")),
                "late_fee": _money_text_or_empty(data.get("delayPayTax")),
            },
            "bi_start_date": proposal_info.get("bi_start_date"),
            "ci_start_date": proposal_info.get("ci_start_date"),
            "commercial_start_date": proposal_info.get("bi_start_date"),
            "compulsory_start_date": proposal_info.get("ci_start_date"),
            "price_items": price_items,
            "premium_total": float(total) if total is not None else None,
            "risk_score": risk_score,
            "result_card": result_card,
            "request_body": request_body,
            "platform_warning": warning,
            "raw_summary": {
                "quotationNo": data.get("quotationNo"),
                "quotationId": data.get("quotationId"),
                "status": _json_obj(quote_response).get("status"),
            },
        }
