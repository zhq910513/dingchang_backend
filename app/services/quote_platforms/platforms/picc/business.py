# encoding: utf-8
from __future__ import annotations

import asyncio
import html
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult, QuotePlatformAdapter
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
JOINT_SALE_PLAN_INFO_PATH = "/khyx/newFront/prpall/common/choosePlanInfoForJointSale.do"
MONOPOLY_QUERY_PATH = "/khyx/newFront/qth/myinfo/monopoly/query.do"
QUERY_QUALITY_FLAG_PATH = "/khyx/newFront/qth/price/queryQualityFlag.do"
GET_CLUB_GIFT_DISPLAY_INFO_PATH = "/khyx/newFront/qth/price/getClubGiftDisplayInfo.do"
QUERY_CAR_CHECKER_PATH = "/khyx/newFront/common/queryCarchecker.do"
GET_CURRENT_TIME_PATH = "/khyx/newFront/price/getCurrentTime.do"
QUOTE_PATH = "/khyx/newFront/qth/price/quote.do"
QUERY_QUOTE_TIMES_PATH = "/khyx/newFront/qth/price/queryQuoteTimes.do"
CLEAR_JS_QUOTATION_NO_PATH = "/khyx/newFront/qth/price/clearJSQuotationNo.do"
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

PICC_REAL_QUOTE_ACCOUNT_TYPES = {
    NEW_FUEL_ACCOUNT_TYPE,
    USED_FUEL_ACCOUNT_TYPE,
    NEW_ENERGY_NEW_ACCOUNT_TYPE,
    NEW_ENERGY_USED_ACCOUNT_TYPE,
}
PICC_MOTOR_QUOTE_PROFILES: Dict[str, Dict[str, Any]] = {
    NEW_FUEL_ACCOUNT_TYPE: {
        "account_type_name": NEW_FUEL_ACCOUNT_TYPE,
        "request_id_prefix": "picc-new-fuel",
        "mode": "picc_new_fuel_real",
        "stub_mode": "picc_new_fuel_preflight_stub",
        "display_name": "人保油车-新报价",
        "energy_type_plat": "0",
        "energy_type_name": "燃油",
        "vehicle_energy_type": "0",
        "is_energy_car": "0",
        "energy_flag": "0",
        "tax_calculate_mode": "C1",
        "vehicle_fuel_type": "D1",
        "fuel_type": "A",
        "new_car_flag": "on",
        "include_pay_last_year": False,
        "license_no_strategy": "new_car_placeholder",
        "enroll_date_fallback": "today",
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
        "stub_mode": "picc_used_fuel_preflight_stub",
        "display_name": "人保油车-旧报价",
        "energy_type_plat": "0",
        "energy_type_name": "燃油",
        "vehicle_energy_type": "0",
        "is_energy_car": "0",
        "energy_flag": "0",
        "tax_calculate_mode": "C1",
        "vehicle_fuel_type": "D1",
        "fuel_type": "A",
        "new_car_flag": "",
        "include_pay_last_year": True,
        "license_no_strategy": "required",
        "enroll_date_fallback": "",
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
        "stub_mode": "picc_new_energy_new_preflight_stub",
        "display_name": "人保新能源车-新报价",
        "energy_type_plat": "1",
        "energy_type_name": "纯电动",
        "vehicle_energy_type": "1",
        "is_energy_car": "1",
        "energy_flag": "1",
        "tax_calculate_mode": "C2",
        "vehicle_fuel_type": "D6",
        "fuel_type": "A",
        "new_car_flag": "on",
        "include_pay_last_year": False,
        "license_no_strategy": "new_car_placeholder",
        "enroll_date_fallback": "today",
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
        "stub_mode": "picc_new_energy_used_preflight_stub",
        "display_name": "人保新能源车-旧报价",
        "energy_type_plat": "1",
        "energy_type_name": "纯电动",
        "vehicle_energy_type": "1",
        "is_energy_car": "1",
        "energy_flag": "1",
        "tax_calculate_mode": "C2",
        "vehicle_fuel_type": "D6",
        "fuel_type": "A",
        "new_car_flag": "",
        "include_pay_last_year": True,
        "license_no_strategy": "required",
        "enroll_date_fallback": "",
        "product_defaults": {
            PRODUCT_COMPULSORY: "20",
            PRODUCT_THIRD_PARTY: "300",
            PRODUCT_DRIVER: "1",
            PRODUCT_PASSENGER: "1",
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
    "车牌颜色代码": "01",
    "国产进口标识": "01",
    "纳税人类型": "01",
    "车辆颜色代码": "999",
    "车主与被保险人关系": "所有",
    "车船税减免类型": "1",
    "车主类型": "1",
    "车主性别": "1",
    "车主生日": "1990-01-01",
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
    }


def _picc_business_defaults(default_values: Any) -> Dict[str, Any]:
    """Merge hidden protocol defaults behind the editable PICC product config."""
    defaults = dict(PICC_COMMON_PLATFORM_DEFAULTS)
    defaults.update(_json_obj(default_values))
    return defaults

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


def _clean_money_text(value: Any, default: str = "0") -> str:
    amount = _money(value, default)
    if amount == amount.to_integral():
        return str(int(amount))
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _clean_money_text_or_empty(value: Any) -> str:
    return _clean_money_text(value) if _has_text(value) else ""


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


def _platform_quote_date_command(value: Any, *, kinds: Optional[List[str]] = None) -> str:
    day = _platform_effective_quote_date(value)
    if not day:
        return ""
    safe_kinds = {item for item in (kinds or ["bi", "ci"]) if item in {"bi", "ci"}}
    lines = []
    if "bi" in safe_kinds:
        lines.append(f"商业起保日期：{day}")
    if "ci" in safe_kinds:
        lines.append(f"交强起保日期：{day}")
    lines.append("人保报价")
    return "\n".join(lines)


def _insurance_date_notice_from_adjustment(adjustment: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "type": "insurance_date_adjust",
        "message": _to_str(adjustment.get("message")).strip(),
        "commercial_start_date": _date_text(adjustment.get("commercial_start_date")),
        "compulsory_start_date": _date_text(adjustment.get("compulsory_start_date")),
        "adjustment_kinds": [
            item
            for item in (adjustment.get("adjustment_kinds") if isinstance(adjustment.get("adjustment_kinds"), list) else [])
            if item in {"bi", "ci"}
        ],
        "source": adjustment.get("source") or "platform_prompt",
    }


def _emit_insurance_date_adjust_notice(callback: Any, adjustment: Mapping[str, Any]) -> bool:
    if not callable(callback):
        return False
    notice = _insurance_date_notice_from_adjustment(adjustment)
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
        advise_start = _first_text(item.get("adviseStartDate"), item.get("effectiveDate"))
        if advise_start:
            lines.extend(
                [
                    "该车辆商业险保险期间与现存有效保单重复投保，",
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


def _insurance_date_error_adjustment_kinds(message: Any) -> List[str]:
    text = _platform_notice_text(message)
    kinds: List[str] = []
    if re.search(r"(?:商业险?|商业).{0,8}起保.{0,80}(?:当前时间|之前|不能)", text):
        kinds.append("bi")
    if re.search(r"(?:交强险?|交强).{0,8}起保.{0,80}(?:当前时间|之前|不能)", text):
        kinds.append("ci")
    return kinds


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
    advise_start = _first_text(first_reinsure.get("adviseStartDate"), first_reinsure.get("effectiveDate"))
    adjusted_start = _platform_effective_quote_date(advise_start)
    adjustment_kinds = _reinsure_adjustment_kinds(first_reinsure) if first_reinsure else []
    confirm_command = _platform_quote_date_command(advise_start, kinds=adjustment_kinds)
    return {
        "type": "confirm" if confirm_command else "notice",
        "subtype": "insurance_date_adjust" if confirm_command else "quote_platform_notice",
        "title": "报价提示",
        "severity": "warning",
        "message": message,
        "confirm_required": bool(confirm_command),
        "confirm_text": "修改保险时间" if confirm_command else "确定",
        "cancel_text": "关闭" if confirm_command else "",
        "close_text": "关闭",
        "confirm_action": {"command": confirm_command} if confirm_command else {},
        "suggested_commercial_start_date": (adjusted_start or _date_text(advise_start)) if "bi" in adjustment_kinds else "",
        "suggested_compulsory_start_date": (adjusted_start or _date_text(advise_start)) if "ci" in adjustment_kinds else "",
        "adjustment_kinds": adjustment_kinds,
        "reinsure_items": reinsure_items[:3] if reinsure_items else [],
    }


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
    vin = _first_text(vin_no, *(_json_obj(row).get("vinNo") for row in rows))
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
    return bool(re.search(r"(重复|已报价|重复报价|已经报价|不能重复)", raw))


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
        "business_status": "duplicate_quote_confirm_required",
        "error_code": "duplicate_quote_confirm_required",
        "duplicate_quote_warning": warning,
        "duplicateVin": duplicate,
        "request_body": request_body,
        "request_body_draft": request_body,
        "offline_request_body": True,
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


def _vehicle_model_code_from_vehicle(vehicle: Mapping[str, Any]) -> str:
    return _compact_vehicle_compare_text(
        _first_text(
            vehicle.get("rawModelName"),
            vehicle.get("vehicleFgwCode"),
            vehicle.get("modelName"),
        )
    )


def _vehicle_candidate_score(row: Mapping[str, Any], vehicle: Mapping[str, Any]) -> int:
    haystack = _vehicle_row_haystack(row)
    model_code = _vehicle_model_code_from_vehicle(vehicle)
    brand = _compact_vehicle_compare_text(_vehicle_brand_prefix(vehicle.get("brandNameHint")))
    name_hint = _compact_vehicle_compare_text(_vehicle_name_hint(vehicle.get("vehicleNameHint")) or vehicle.get("energyModelSuffix"))
    score = 0
    if model_code and model_code in haystack:
        score += 100
    if brand and brand in haystack:
        score += 20
    if name_hint and name_hint in haystack:
        score += 15
    energy_expected = bool(re.search(r"(纯电|电动|新能源|BEV|PHEV|EV)", _compact_vehicle_compare_text(vehicle.get("energyModelSuffix"))))
    if energy_expected and (
        re.search(r"(纯电|电动|新能源|BEV|PHEV|EV)", haystack)
        or _to_str(row.get("energyTypePlat")).strip() == "1"
        or _to_str(row.get("isEnergyCar")).strip() in {"1", "true", "True"}
    ):
        score += 10
    return score


def _is_no_data_platform_response(data: Any) -> bool:
    raw = _to_str(data)
    return bool(re.search(r"(无数据返回|无数据|未查询到|没有查询到|暂无数据|没有数据)", raw))


def _pick_highest_price_vehicle(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=_vehicle_price)


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


PICC_PROPOSAL_KIND_NAME_BY_CODE = {
    "051050": "机动车损失保险",
    "051051": "机动车第三者责任保险",
    "051052": "机动车车上人员责任保险（司机）",
    "051053": "机动车车上人员责任保险（乘客）",
    "051063": "附加医保外医疗费用责任险（机动车第三者责任保险）",
    "051064": "附加机动车增值服务特约条款（道路救援服务）",
    "051074": "交强险",
    "051085": "附加外部电网故障损失险",
}

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


def _proposal_wan_text(value: Any, default: str = "-") -> str:
    amount = _money(value)
    if amount <= 0:
        return default
    wan = amount / Decimal("10000")
    return f"{_clean_money_text(wan)}万元"


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


def _proposal_kind_amount_text(row: Mapping[str, Any], *, seat_count: Any = "", shared_main_limit: Optional[bool] = None) -> str:
    code = _to_str(row.get("kindCode")).strip()
    amount = _money(row.get("amount"))
    unit_amount = _money(row.get("unitAmount"))
    seats = _safe_int_local(seat_count, 0) if _has_text(seat_count) else 0
    if code == "051063" and shared_main_limit is True:
        return "共享主险限额"
    if amount <= 0:
        return "-"
    if code == "051050":
        return _proposal_money_yuan(amount, keep_decimal=True)
    if code == "051053":
        per_seat = unit_amount if unit_amount > 0 else amount / Decimal(max(seats - 1, 1))
        if unit_amount <= 0 and seats <= 1:
            return _proposal_money_yuan(amount, keep_decimal=True)
        quantity = max(1, int((amount / per_seat).quantize(Decimal("1"), rounding=ROUND_HALF_UP))) if per_seat else max(seats - 1, 1)
        return f"{_proposal_wan_text(per_seat).replace('万元', '')}万元/座*{quantity}"
    if code in {"051051", "051052", "051063", "051074", "051085"}:
        return _proposal_wan_text(amount)
    return _proposal_money_yuan(amount, keep_decimal=True)


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
    body = dict(_json_obj(request_body))
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
    set_form("prpCitemCar.vehicleFuelType", _first_text(precise_vehicle.get("vehicleFuelType"), selected.get("vehicleFuelType")))
    set_form("prpCmain.vehicleStyleUniqueId", _first_text(selected.get("vehicleStyleUniqueId"), vehicle.get("vehicleStyleUniqueId")))
    set_form("prpCmain.presaleCarFlag", _first_text(precise_vehicle.get("presaleCarFlag"), selected.get("presaleCarFlag")))

    defaults = _json_obj(body.get("defaultFields"))
    if not _product_excluded(defaults, PRODUCT_LOSS) and not _to_str(_default_value(defaults, PRODUCT_LOSS)).strip():
        set_form("prpCitemKindVos[1].amount", _money_text(actual_value))

    set_vehicle("purchasePrice", purchase_price)
    set_vehicle("actualValue", _money_text(actual_value))
    set_vehicle("modelCode", model_code)
    set_vehicle("platModelCode", vehicle_model_code)
    set_vehicle("selectedModelName", selected_model_name)
    set_vehicle("selectedVehicleId", model_code)
    set_vehicle("vehicleFgwCode", _model_search_code(vehicle_fgw_code))
    set_vehicle("platformBrandId", brand_id)
    set_vehicle("platformBrandIDNew", brand_id_new)

    preflight["vehicleModelAutoAccepted"] = {
        "accepted": True,
        "reason": "平台提示车型不一致，已自动使用精确车型确认接口返回值重试一次",
        "brandId": brand_id,
        "vehicleName": selected_model_name,
        "vehicleId": model_code,
        "vehicleModelCode": vehicle_model_code,
        "purchasePrice": purchase_price,
    }
    body["quoteForm"] = form
    body["vehicleForm"] = vehicle
    body["preflight"] = preflight
    return body, changed


def _vehicle_model_suffix_from_type(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value))
    for suffix in ("轿车", "客车", "货车", "越野车", "牵引车", "专项作业车", "摩托车", "挂车"):
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
    return text.strip()


def _vehicle_name_hint(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    if not text:
        return ""
    if re.fullmatch(r"[A-Z0-9_-]{4,}", text, flags=re.I):
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
        text = re.sub(r"\s+", "", _to_str(value).strip()).strip("*")
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _used_fuel_model_query_terms(
    model_name: Any,
    vehicle_type: Any = "",
    energy_model_suffix: Any = "",
    *,
    brand_name: Any = "",
    vehicle_name: Any = "",
) -> List[str]:
    raw = re.sub(r"\s+", "", _to_str(model_name).strip()).strip("*")
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
    brand = _vehicle_brand_prefix(brand_name)
    name_hint = _vehicle_name_hint(vehicle_name) or energy_suffix
    brand_typed = _join_model_term(brand, no_brand_suffix, name_hint)
    brand_plain = _join_model_term(brand, no_brand_suffix)
    named_plain = _join_model_term("", no_brand_suffix, name_hint)
    return _dedupe_model_terms([brand_typed, brand_plain, named_plain, energy_typed, typed, no_brand_suffix, raw])


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
        body = dict(_json_obj(request_body))
        if not codes:
            return body, False
        defaults = _json_obj(body.get("defaultFields"))
        vehicle = dict(_json_obj(body.get("vehicleForm")))
        owner = dict(_json_obj(body.get("ownerForm")))
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
        rows = _vehicle_rows(self._query_vehicle_candidates(client, vehicle))
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
        preflight.update(
            {
                "selectedVehicle": selected,
                "preciseVehicle": precise_vehicle,
                "actualValue": actual_value_result,
                "vehicleModelAutoAccepted": {
                    "accepted": True,
                    "reason": "平台提示车型不一致，已自动使用平台返回车型码重试一次",
                    "platformReturnedCode": selected_code,
                    "vehicleName": quote_form.get("prpCitemCar.brandName"),
                    "vehicleId": quote_form.get("prpCitemCar.modelCode"),
                    "vehicleModelCode": quote_form.get("prpCmain.vehicleModelCode"),
                    "purchasePrice": quote_form.get("prpCitemCar.purchasePrice"),
                    "selectedBy": _vehicle_selection_rule(explicit_loss_amount),
                    "requestedLossAmount": _clean_money_text(explicit_loss_amount) if explicit_loss_amount > 0 else "",
                    "lossThresholdPurchasePrice": _clean_money_text(_vehicle_loss_threshold(explicit_loss_amount)) if explicit_loss_amount > 0 else "",
                },
            }
        )
        body["vehicleForm"] = vehicle
        body["productForm"] = {
            "products": products,
            "sharedMainLimit": _checked(_default_value(defaults, PRODUCT_SHARED_LIMIT, True), default=True),
        }
        body["quoteForm"] = quote_form
        body["preflight"] = preflight
        return body, True

    def _quote_sync(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        client: Optional[PiccProtocolClient] = None
        real_account_type = self._real_quote_account_type(ctx, quote_payload)
        is_real_quote = bool(real_account_type)
        request_body_draft: Dict[str, Any] = {}
        draft_error = ""
        request_body: Dict[str, Any] = {}
        runtime_stage = "init"
        try:
            request_body_draft = (
                self._assemble_used_fuel_offline_request_body(ctx, quote_payload, account_type_name=real_account_type)
                if is_real_quote
                else self._assemble_stub_request_body(ctx, quote_payload)
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
                request_body = self._prepare_used_fuel_quote(client, ctx, quote_payload, account_type_name=real_account_type)
                duplicate_confirm_payload = _duplicate_quote_confirmation_payload(request_body)
                if duplicate_confirm_payload and not _duplicate_quote_confirmed(quote_payload, request_body):
                    return PlatformRuntimeResult(
                        status="duplicate_quote_confirm_required",
                        message=_to_str(duplicate_confirm_payload.get("duplicate_quote_warning")),
                        data=success_data(client, extra=duplicate_confirm_payload),
                    )
                runtime_stage = "submit_quote"
                request_body, quote_response, auto_period_notices = self._submit_used_fuel_quote_with_period_auto_adjust(
                    client,
                    request_body,
                    auto_notice_callback=_json_obj(ctx.payload).get("auto_notice_callback"),
                )
                runtime_stage = "build_quote_result"
                quote_result = self._build_used_fuel_quote_result_from_response(ctx, quote_payload, request_body, quote_response)
                if auto_period_notices:
                    quote_result["platform_auto_notices"] = auto_period_notices
                platform_dialog = _used_fuel_quote_platform_dialog(quote_response)
                if platform_dialog and not (
                    auto_period_notices
                    and _to_str(platform_dialog.get("subtype")).strip().lower() == "insurance_date_adjust"
                ):
                    quote_result["platform_dialog"] = platform_dialog
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
                        "mode": quote_result.get("mode") or "picc_protocol_stub",
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
            if client is not None:
                data_payload = success_data(client, extra=data_payload)
            return PlatformRuntimeResult(
                status="duplicate_quote",
                message=str(exc) or "平台提示该车辆已报价过",
                data=data_payload,
            )
        except PiccQuotaFullError as exc:
            data_payload = {"business_status": "quota_full", "error_code": "quota_full"}
            if client is not None:
                data_payload = success_data(client, extra=data_payload)
            return PlatformRuntimeResult(
                status="quota_full",
                message=str(exc) or "查询额度已用完",
                data=data_payload,
            )
        except PiccSessionExpiredError as exc:
            return PlatformRuntimeResult(
                status="expired",
                message=str(exc) or "PICC 登录已过期，请重新登录",
                data={
                    "business_status": "16",
                    "error_code": exc.__class__.__name__,
                    "request_body": request_body or request_body_draft,
                    "request_body_draft": request_body_draft,
                    "offline_request_body": True,
                    "request_body_error": draft_error,
                },
            )
        except PiccTransientGatewayError as exc:
            return PlatformRuntimeResult(
                status="network_error",
                message=str(exc) or "PICC 平台网关临时异常，请稍后重试",
                data={
                    "error_code": exc.__class__.__name__,
                    "transient": True,
                    "request_body": request_body or request_body_draft,
                    "request_body_draft": request_body_draft,
                    "offline_request_body": True,
                    "request_body_error": draft_error,
                },
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
                auto_notices = getattr(exc, "platform_auto_notices", None)
                if auto_notices:
                    data_payload["platform_auto_notices"] = [dict(item or {}) for item in auto_notices if isinstance(item, Mapping)]
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
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={
                    "error_code": exc.__class__.__name__,
                    "error_stage": runtime_stage,
                    "request_body": request_body or request_body_draft,
                    "request_body_draft": request_body_draft,
                    "offline_request_body": True,
                    "request_body_error": draft_error,
                },
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
        vin_no = _first_text(data.get("vinNo"), data.get("frameNo"), form.get("prpCitemCar.vinNo"))
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

    def _assemble_stub_request_body(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> Dict[str, Any]:
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
        body["applicant"] = _deep_merge(
            _json_obj(body.get("applicant")),
            {
                "name": normalized_data.get("owner_name") or normalized_data.get("id_name"),
                "phone": normalized_data.get("owner_phone"),
                "idNo": normalized_data.get("id_number"),
            },
        )
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
        return {
            "ownerName": _first_text(normalized_data.get("owner_name"), normalized_data.get("id_name"), _field_value(defaults, "车主")),
            "ownerIdNo": _first_text(normalized_data.get("id_number"), _field_value(defaults, "车主证件号码")),
            "ownerPhone": _first_text(normalized_data.get("owner_phone"), _field_value(defaults, "车主手机号")),
        }

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
        owner = _deep_merge(
            self._used_fuel_owner(defaults, normalized_data),
            _json_obj(incoming_body.get("ownerForm")),
        )
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

        return {
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
        }

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
        vehicle = self._base_used_fuel_vehicle(defaults, normalized_data, profile=profile)
        owner = self._used_fuel_owner(defaults, normalized_data)

        search_result = self._query_vehicle_candidates(client, vehicle)
        candidates = _vehicle_rows(search_result)
        selected = _pick_best_vehicle_candidate(candidates, vehicle, explicit_loss_amount=explicit_loss_amount)
        if not selected:
            tried_terms = [
                _to_str(item).strip().rstrip("*")
                for item in (vehicle.get("modelQueryTerms") if isinstance(vehicle.get("modelQueryTerms"), list) else [])
                if _to_str(item).strip()
            ]
            tried_text = f"，已尝试：{'、'.join(tried_terms[:6])}" if tried_terms else ""
            raise PiccRequestError(f"车型名称【{vehicle.get('rawModelName') or vehicle.get('modelName') or '-'}】未查询到可用车型配置{tried_text}")

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
        checker_info = self._query_car_checker(client, defaults)
        if checker_info:
            vehicle["carchecker"] = _first_text(checker_info.get("userName"), vehicle.get("carchecker"))
            vehicle["mainComCode"] = _first_text(checker_info.get("comCode"), vehicle.get("mainComCode"))
        products = self._used_fuel_products(defaults, profile=profile, actual_value=actual_value, seat_count=vehicle.get("seatCount"))
        quote_form = self._build_used_fuel_quote_form(defaults, vehicle, owner, selected, precise_vehicle, products, profile=profile)
        prechecks = self._run_used_fuel_quote_prechecks(client, defaults, quote_form)
        joint_sale = self._query_tujia_anshun_plan_best_effort(client, defaults, quote_form)

        return {
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
                    "requestedLossAmount": _clean_money_text(explicit_loss_amount) if explicit_loss_amount > 0 else "",
                    "lossThresholdPurchasePrice": _clean_money_text(_vehicle_loss_threshold(explicit_loss_amount)) if explicit_loss_amount > 0 else "",
                },
                "selectedVehicle": selected,
                "preciseVehicle": precise_vehicle,
                "actualValue": actual_value_result,
                "taxabate": taxabate_result,
                "carChecker": checker_info,
                "quotePrechecks": prechecks,
                "jointSale": {
                    "tujiaAnshun": joint_sale,
                },
            },
        }

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

    def _run_used_fuel_quote_prechecks(
        self,
        client: PiccProtocolClient,
        defaults: Mapping[str, Any],
        quote_form: Mapping[str, Any],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
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

        vin = _to_str(quote_form.get("prpCitemCar.vinNo")).strip()
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
        end_date_ci = _end_date_text(start_date_ci)
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
            "prpCmain.starthourbi": "0",
            "prpCmain.startminutebi": "0",
            "prpCmain.endhourbi": "24",
            "prpCmain.endminutebi": "0",
            "prpCmain.startDateCI": start_date_ci,
            "prpCmain.starthourci": "0",
            "prpCmain.startminuteci": "0",
            "prpCmain.endDateCI": end_date_ci,
            "prpCmain.endhourci": "24",
            "prpCmain.endminuteci": "0",
            "prpCmain.custAuthorization": "0",
            "prpCmain.vehicleModelCode": vehicle_model_code,
            "prpCmain.insuredChooseUsedName": "0",
            "prpCmain.carOwnerChooseUsedName": "0",
            "businesNature": _to_str(_field_value(defaults, "业务性质代码", "businesNature", fallback="2")),
            "businesNatureName": _to_str(_field_value(defaults, "业务性质名称", "businesNatureName", fallback="专业代理业务")),
            "energyTypePlat": _to_str(_field_value(defaults, "能源类型代码", "energyTypePlat", fallback=_profile_text(prof, "energy_type_plat", "0"))),
            "energyTypePlatTemp": _to_str(_field_value(defaults, "能源类型名称", "energyTypePlatTemp", fallback=_profile_text(prof, "energy_type_name", "燃油"))),
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
            "prpCitemCar.licenseType": _to_str(vehicle.get("licenseType") or "02"),
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
            "prpCitemCar.energyType": _to_str(_field_value(defaults, "车辆能源类型", "energyType", fallback=_profile_text(prof, "vehicle_energy_type", "0"))),
            "prpCitemCar.referenceActualValue": actual_value,
            "prpCitemCar.queryArea": query_area,
            "prpCitemCar.carInsuredRelation": _to_str(_field_value(defaults, "车主与被保险人关系", "carInsuredRelation", fallback="所有")),
            "prpCitemCar.loanVehicleFlag": "0",
            "prpCitemCar.clauseType": _to_str(_field_value(defaults, "条款类型", "clauseType", fallback="F42")),
            "prpCitemCar.licenseColorCode": _to_str(_field_value(defaults, "车牌颜色代码", "licenseColorCode", fallback="01")),
            "prpCitemCar.netWeifaFlag": "0",
            "prpCitemCar.isEnergyCar": _profile_text(prof, "is_energy_car", "0"),
            "prpCitemCar.isDangerousCar": "0",
            "prpCitemCar.IsCriterion": "1",
            "prpCitemCar.taxPayerType": _to_str(_field_value(defaults, "纳税人类型", "taxPayerType", fallback="01")),
            "prpCitemCar.fuelType": _to_str(_field_value(defaults, "燃料种类", "fuelType", fallback=_profile_text(prof, "fuel_type", "A"))),
            "prpCitemCar.vehicleFuelType": _first_text(selected.get("vehicleFuelType"), _field_value(defaults, "车辆燃料类型", "vehicleFuelType", fallback=_profile_text(prof, "vehicle_fuel_type", "D1"))),
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
            "prpCcarShipTax.taxType": _to_str(_field_value(defaults, "车船税类型", "taxType", fallback="1")),
            "prpCcarShipTax.calculateMode": _to_str(_field_value(defaults, "车船税计算方式", "calculateMode", fallback=_profile_text(prof, "tax_calculate_mode", "C1"))),
            "prpCcarShipTax.taxcomcode": _to_str(_field_value(defaults, "税务机关代码", "taxcomcode")),
            "prpCcarShipTax.taxcomname": _to_str(_field_value(defaults, "税务机关名称", "taxcomname")),
            "prpCcarShipTax.taxAbateType": _to_str(_field_value(defaults, "车船税减免类型", "taxAbateType", fallback="1")),
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
            "energyFlag": _profile_text(prof, "energy_flag", "0"),
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
        product_specs = [
            (compulsory_amount, "051074", PRODUCT_COMPULSORY),
            (loss_amount, "051050", PRODUCT_LOSS),
            (third_party_amount, "051051", PRODUCT_THIRD_PARTY),
            (driver_amount, "051052", PRODUCT_DRIVER),
            (passenger_amount, "051053", PRODUCT_PASSENGER),
            (medical_third_amount, "051063", PRODUCT_MEDICAL_THIRD),
        ]
        excluded_products = _product_exclusions(defaults)
        product_rows = [
            (amount, kind_code, kind_name)
            for amount, kind_code, kind_name in product_specs
            if _canonical_product_name(kind_name) not in excluded_products
        ]
        medical_third_index: Optional[int] = None
        for index, (amount, kind_code, kind_name) in enumerate(product_rows):
            form[f"prpCitemKindVos[{index}].amount"] = amount
            form[f"prpCitemKindVos[{index}].kindCode"] = kind_code
            form[f"prpCitemKindVos[{index}].kindName"] = kind_name
            form[f"prpCitemKindVos[{index}].chooseFlag"] = "true"
            if kind_name == PRODUCT_MEDICAL_THIRD:
                medical_third_index = index
        if medical_third_index is not None:
            form[f"prpCitemKindVos[{medical_third_index}].sharedAmountFlag"] = "1" if shared_main_limit else "0"
        for key in USED_FUEL_QUOTE_EMPTY_FORM_FIELDS:
            form.setdefault(key, "")
        # Allow advanced platform-specific overrides without changing the schema.
        extra_form = _json_obj_loose(defaults.get("PICC报价请求体覆盖") or defaults.get("quoteFormOverrides"))
        for key, value in extra_form.items():
            if _to_str(key).strip():
                form[_to_str(key).strip()] = value
        return {key: value for key, value in form.items() if value is not None}

    def _submit_used_fuel_quote(self, client: PiccProtocolClient, request_body: Mapping[str, Any]) -> Dict[str, Any]:
        form_body = _json_obj(request_body.get("quoteForm"))
        if not form_body:
            raise PiccRequestError("人保报价请求体为空，无法提交报价")
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
            if _contains_duplicate_quote(data):
                raise PiccDuplicateQuoteError(message or "平台提示该车辆已报价过")
            if _contains_quota_full(data):
                raise PiccQuotaFullError(message or "查询额度已用完")
            if _quote_response_has_display_result(data):
                return _json_obj(data)
            raise PiccBusinessRequestError(
                f"报价提交失败：{message}",
                action="报价提交",
                platform_response=data,
                request_body=form_body,
            )
        payload = _json_obj(_json_obj(data).get("data"))
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
        raw_model_name = _first_text(data.get("vehicle_model"), _field_value(defaults, "车型名称", "品牌型号"))
        vehicle_brand_name = _first_text(
            data.get("vehicle_brand_name"),
            _field_value(defaults, "车辆品牌/车辆名称", "车辆品牌", "品牌名称"),
        )
        vehicle_name_hint = _first_text(
            data.get("vehicle_name"),
            data.get("car_name"),
            _field_value(defaults, "车辆名称"),
        )
        engine_no = _first_text(data.get("engine_no"), _field_value(defaults, "发动机号"))
        vin = _first_text(data.get("vin"), _field_value(defaults, "VIN/车架号", "车架号"))
        license_no = _first_text(data.get("plate_no"), _field_value(defaults, "号牌号码"))
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
        model_terms = _used_fuel_model_query_terms(
            raw_model_name,
            vehicle_type,
            "纯电动轿车" if _profile_text(prof, "is_energy_car") == "1" else "",
            brand_name=vehicle_brand_name,
            vehicle_name=vehicle_name_hint,
        )
        return {
            "licenseNo": license_no,
            "licenseType": _first_text(_field_value(defaults, "号牌种类"), "02"),
            "engineNo": engine_no,
            "vin": vin,
            "transferDate": transfer_date,
            "carKindCode": _first_text(_field_value(defaults, "车辆种类"), "A01"),
            "useNatureCode": _first_text(_field_value(defaults, "使用性质细分种类", "使用性质"), "211"),
            "enrollDate": enroll_date,
            "startDateBI": _first_text(_date_text(data.get("commercial_start_date")), _date_text(_field_value(defaults, "商业起保日期")), next_day),
            "startDateCI": _first_text(_date_text(data.get("compulsory_start_date")), _date_text(_field_value(defaults, "交强起保日期")), next_day),
            "modelName": model_terms[0] if model_terms else raw_model_name,
            "rawModelName": raw_model_name,
            "brandNameHint": vehicle_brand_name,
            "vehicleNameHint": vehicle_name_hint,
            "vehicleType": vehicle_type,
            "energyModelSuffix": "纯电动轿车" if _profile_text(prof, "is_energy_car") == "1" else "",
            "seatCount": _first_text(data.get("approved_passenger_count"), _field_value(defaults, "座位数"), "5"),
        }

    def _query_vehicle_candidates(self, client: PiccProtocolClient, vehicle: Mapping[str, Any]) -> Any:
        model_name = _to_str(vehicle.get("rawModelName") or vehicle.get("modelName")).strip()
        if not model_name:
            raise PiccRequestError("人保报价缺少车型名称，无法查询车型配置")
        terms = _used_fuel_model_query_terms(
            model_name,
            vehicle.get("vehicleType"),
            vehicle.get("energyModelSuffix"),
            brand_name=vehicle.get("brandNameHint"),
            vehicle_name=vehicle.get("vehicleNameHint"),
        ) or [model_name]
        last_data: Any = {}
        for term in terms:
            params = {
                "jyVehicleRequest.resources": "0524",
                "jyVehicleRequest.brandName": "",
                "jyVehicleRequest.vinno": vehicle.get("vin") or "",
                "jyVehicleRequest.vehicleName": term if term.endswith("*") else f"{term}*",
                "jyVehicleRequest.vehicleAlias": "",
                "jyVehicleRequest.vehicleId": "",
                "jyVehicleRequest.searchCode": "",
                "jyVehicleRequest.platModelCode": "",
                "page": 1,
                "rows": 10,
            }
            data = client.request_json(
                "GET",
                VEHICLE_QUERY_PATH,
                purpose="business",
                params=params,
                headers={"Referer": f"{client.config.base_url}/khyxui/homePage"},
            )
            last_data = data
            try:
                _ensure_platform_success(data, action="车型配置查询")
            except PiccBusinessRequestError:
                if _is_no_data_platform_response(data):
                    continue
                raise
            if _vehicle_rows(data):
                if isinstance(vehicle, dict):
                    vehicle["modelName"] = term
                    vehicle["modelQueryTerms"] = terms
                    vehicle["modelQueryMatched"] = term
                return data
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
        prof = _json_obj(profile)
        purchase_price = _first_text(selected.get("purchasePrice"), selected.get("priceP"), selected.get("priceT"))
        params = {
            "vin": vehicle.get("vin") or "",
            "startDate": vehicle.get("startDateBI") or _next_day_text(),
            "startHour": 0,
            "startMinute": 0,
            "licenseNo": vehicle.get("licenseNo") or "",
            "licenseType": vehicle.get("licenseType") or "02",
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
            "energyTypePlat": _first_text(selected.get("energyTypePlat"), _profile_text(prof, "precise_energy_type_plat")),
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
        prof = _json_obj(profile)
        params = {
            "energyTypePlat": _first_text(
                precise_vehicle.get("energyTypePlat"),
                selected.get("energyTypePlat"),
                _field_value(defaults, "能源类型代码", "energyTypePlat"),
                _profile_text(prof, "energy_type_plat", "0"),
            ),
            "energyFlag": _field_value(defaults, "能源标识", "energyFlag", fallback=_profile_text(prof, "energy_flag", "0")),
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
    ) -> Dict[str, Any]:
        dialog = _used_fuel_quote_platform_dialog(platform_response)
        message = _to_str(dialog.get("message")).strip()
        kinds = [item for item in (dialog.get("adjustment_kinds") if isinstance(dialog.get("adjustment_kinds"), list) else []) if item in {"bi", "ci"}]
        reinsure_items = dialog.get("reinsure_items") if isinstance(dialog.get("reinsure_items"), list) else []
        first_reinsure = _json_obj(reinsure_items[0]) if reinsure_items else {}
        candidate_date = _first_text(
            first_reinsure.get("adviseStartDate"),
            first_reinsure.get("effectiveDate"),
            dialog.get("suggested_commercial_start_date"),
            dialog.get("suggested_compulsory_start_date"),
        )

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

        if not kinds:
            return {}

        platform_next_day = self._platform_next_quote_start_date(client)
        start_day = _platform_effective_quote_date(candidate_date or platform_next_day, min_day=platform_next_day)
        if not start_day:
            start_day = platform_next_day
        return {
            "message": message or "平台提示需要修改保险期间，已按平台当前时间自动调整。",
            "start_date": start_day,
            "commercial_start_date": start_day if "bi" in kinds else "",
            "compulsory_start_date": start_day if "ci" in kinds else "",
            "adjustment_kinds": kinds,
            "source": "platform_current_time_and_quote_prompt",
        }

    def _apply_insurance_date_adjustment_to_request_body(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
        adjustment: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], bool, Dict[str, Any]]:
        body = dict(_json_obj(request_body))
        form = dict(_json_obj(body.get("quoteForm")))
        vehicle = dict(_json_obj(body.get("vehicleForm")))
        owner = dict(_json_obj(body.get("ownerForm")))
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
        changed = False

        if "bi" in kinds and bi_day:
            if _to_str(form.get("prpCmain.startDate")).strip() != bi_day:
                form["prpCmain.startDate"] = bi_day
                changed = True
            if _to_str(vehicle.get("startDateBI")).strip() != bi_day:
                vehicle["startDateBI"] = bi_day
                changed = True
        if "ci" in kinds and ci_day:
            if _to_str(form.get("prpCmain.startDateCI")).strip() != ci_day:
                form["prpCmain.startDateCI"] = ci_day
                changed = True
            if _to_str(vehicle.get("startDateCI")).strip() != ci_day:
                vehicle["startDateCI"] = ci_day
                changed = True
            form["prpCmain.endDateCI"] = _end_date_text(ci_day)

        recalculated_actual_value = ""
        if changed and "bi" in kinds and selected:
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

        if not changed:
            return body, False, {}

        notice = {
            "type": "insurance_date_adjust",
            "message": _to_str(adjustment.get("message")).strip(),
            "commercial_start_date": bi_day,
            "compulsory_start_date": ci_day,
            "adjustment_kinds": kinds,
            "actual_value": recalculated_actual_value,
            "source": adjustment.get("source") or "platform_prompt",
        }
        preflight["insuranceDateAutoAdjusted"] = notice
        body["quoteForm"] = form
        body["vehicleForm"] = vehicle
        body["ownerForm"] = owner
        body["defaultFields"] = defaults
        body["preflight"] = preflight
        return body, True, notice

    def _submit_used_fuel_quote_with_vehicle_retry(
        self,
        client: PiccProtocolClient,
        request_body: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        body = dict(_json_obj(request_body))
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
        body = dict(_json_obj(request_body))
        try:
            body, quote_response = self._submit_used_fuel_quote_with_vehicle_retry(client, body)
        except PiccBusinessRequestError as exc:
            adjustment = self._insurance_date_adjustment_from_platform_response(
                client,
                getattr(exc, "platform_response", None),
                error_message=str(exc),
            )
            adjusted_body, changed, notice = self._apply_insurance_date_adjustment_to_request_body(client, body, adjustment)
            if not changed:
                raise
            emitted = _emit_insurance_date_adjust_notice(auto_notice_callback, adjustment)
            if emitted:
                notice["emitted_to_chat"] = True
            notices.append(notice)
            try:
                body, quote_response = self._submit_used_fuel_quote_with_vehicle_retry(client, adjusted_body)
            except PiccBusinessRequestError as retry_exc:
                retry_exc.platform_auto_notices = [
                    *getattr(retry_exc, "platform_auto_notices", []),
                    *notices,
                ]
                raise

        # HAR confirms the page may show "修改保险期间" after a quote response:
        # it fills a platform-current valid start date, recalculates actual value,
        # then submits quote.do again. Keep this bounded to avoid loops.
        for _ in range(2):
            adjustment = self._insurance_date_adjustment_from_platform_response(client, quote_response)
            adjusted_body, changed, notice = self._apply_insurance_date_adjustment_to_request_body(client, body, adjustment)
            if not changed:
                if adjustment and adjustment.get("message") and not notices:
                    ready_notice = _insurance_date_notice_from_adjustment(adjustment)
                    ready_notice["already_effective"] = True
                    emitted = _emit_insurance_date_adjust_notice(auto_notice_callback, adjustment)
                    if emitted:
                        ready_notice["emitted_to_chat"] = True
                    notices.append(ready_notice)
                break
            emitted = _emit_insurance_date_adjust_notice(auto_notice_callback, adjustment)
            if emitted:
                notice["emitted_to_chat"] = True
            notices.append(notice)
            try:
                body, quote_response = self._submit_used_fuel_quote_with_vehicle_retry(client, adjusted_body)
            except PiccBusinessRequestError as retry_exc:
                retry_exc.platform_auto_notices = [
                    *getattr(retry_exc, "platform_auto_notices", []),
                    *notices,
                ]
                raise

        return body, quote_response, notices

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
        rows = [
            {"code": "CI", "name": PRODUCT_COMPULSORY, "required": True, "coverage": _to_str(_profile_product_default(defaults, prof, PRODUCT_COMPULSORY, "20"))},
            {"code": "BI050", "name": PRODUCT_LOSS, "required": True, "insuredAmount": _money_text(loss_amount)},
            {"code": "BI051", "name": PRODUCT_THIRD_PARTY, "required": True, "insuredAmount": third_party_amount},
            {"code": "BI060", "name": PRODUCT_DRIVER, "required": True, "insuredAmount": driver_amount},
            {"code": "BI061", "name": PRODUCT_PASSENGER, "required": True, "insuredAmount": passenger_amount},
            {"code": "BI_SHARED", "name": PRODUCT_SHARED_LIMIT, "required": True, "checked": shared_main_limit},
            {"code": "BI_MEDICAL_THIRD", "name": PRODUCT_MEDICAL_THIRD, "required": True, "insuredAmount": medical_third_amount},
        ]
        exclusions = _product_exclusions(defaults)
        if exclusions:
            rows = [row for row in rows if _canonical_product_name(row.get("name")) not in exclusions]
        return rows

    def _display_kind_name(self, kind_name: Any) -> str:
        text = _to_str(kind_name).strip()
        replacements = {
            "机动车第三者责任保险": "第三者责任险",
            "机动车车上人员责任保险（司机）": PRODUCT_DRIVER,
            "机动车车上人员责任保险（乘客）": PRODUCT_PASSENGER,
            "附加医保外医疗费用责任险（机动车第三者责任保险）": PRODUCT_MEDICAL_THIRD,
            "交强险": "交强险",
        }
        return replacements.get(text, text)

    def _build_used_fuel_quote_result_from_response(
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
        data = _json_obj(_json_obj(quote_response).get("data"))
        vehicle = _json_obj(request_body.get("vehicleForm"))
        owner = _json_obj(request_body.get("ownerForm"))
        form = _json_obj(request_body.get("quoteForm"))
        shared_main_limit = _quote_form_shared_main_limit(form)
        preflight = _json_obj(request_body.get("preflight"))
        selected_vehicle = _json_obj(preflight.get("selectedVehicle"))
        precise_vehicle = _json_obj(preflight.get("preciseVehicle"))
        item_rows = _json_obj(quote_response).get("itemKindTempList")
        if not isinstance(item_rows, list):
            item_rows = data.get("itemKindTempList")
        if not isinstance(item_rows, list):
            item_rows = []

        coverage_items: List[Dict[str, Any]] = []
        commercial_premium_from_rows = Decimal("0")
        commercial_premium_rows_present = False
        compulsory_premium_value: Optional[Decimal] = _money(data.get("ciPremium")) if _has_text(data.get("ciPremium")) else None
        for row_any in item_rows:
            row = _json_obj(row_any)
            kind_code = _to_str(row.get("kindCode")).strip()
            name = self._display_kind_name(row.get("kindName"))
            premium_present = _has_text(row.get("premium"))
            premium = _money(row.get("premium")) if premium_present else Decimal("0")
            if kind_code == "051074" or name == "交强险":
                if compulsory_premium_value is None and premium_present:
                    compulsory_premium = premium
                    compulsory_premium_value = premium
                continue
            if premium_present:
                commercial_premium_from_rows += premium
                commercial_premium_rows_present = True
            coverage_items.append(
                {
                    "code": kind_code,
                    "name": name,
                    "amount": _clean_money_text_or_empty(row.get("amount")),
                    "amount_text": _proposal_kind_amount_text(
                        row,
                        seat_count=_first_text(vehicle.get("seatCount"), form.get("prpCitemCar.seatCount")),
                        shared_main_limit=shared_main_limit,
                    ),
                    "premium": _money_text_or_empty(row.get("premium")),
                    "premium_text": _proposal_money_yuan(row.get("premium")),
                }
            )

        commercial_premium_value: Optional[Decimal] = (
            _money(data.get("biPremium"))
            if _has_text(data.get("biPremium"))
            else (commercial_premium_from_rows if commercial_premium_rows_present else None)
        )
        vehicle_tax_raw = _first_text(data.get("sumPayTax"), data.get("thisPayTax"), data.get("carShipTaxes"))
        vehicle_tax_value: Optional[Decimal] = _money(vehicle_tax_raw) if _has_text(vehicle_tax_raw) else None
        if vehicle_tax_value is None and (_has_text(data.get("prePayTax")) or _has_text(data.get("delayPayTax"))):
            vehicle_tax_value = _money(data.get("prePayTax")) + _money(data.get("delayPayTax"))

        risk_score: Any = ""
        picc_score = _to_str(data.get("piccScore")).strip()
        if picc_score:
            risk_score = _safe_int_local(picc_score, 0)
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
        platform_joint_sales_premium_present = _has_text(data.get("sumYelPremium"))
        platform_joint_sales_premium = _money(data.get("sumYelPremium")) if platform_joint_sales_premium_present else Decimal("0")
        joint_sales_premium_present = platform_joint_sales_premium_present
        joint_sales_premium = platform_joint_sales_premium
        tujia_premium = _money(tujia_anshun.get("premium"))
        joint_sales_premium_from_plan = False
        if tujia_premium > 0 and (not joint_sales_premium_present or joint_sales_premium <= 0):
            joint_sales_premium = tujia_premium
            joint_sales_premium_present = True
            joint_sales_premium_from_plan = True
        joint_sales_amount = _money(tujia_anshun.get("amount")) if _has_text(tujia_anshun.get("amount")) else Decimal("0")
        joint_sales_amount_present = joint_sales_premium > 0 and joint_sales_amount > 0
        if _has_text(data.get("sumPremium")):
            total_without_vehicle_tax: Optional[Decimal] = _money(data.get("sumPremium"))
            if joint_sales_premium_from_plan and platform_joint_sales_premium <= 0:
                total_without_vehicle_tax += joint_sales_premium
        elif commercial_premium_value is not None and compulsory_premium_value is not None:
            total_without_vehicle_tax = commercial_premium_value + compulsory_premium_value + (joint_sales_premium if joint_sales_premium_present else Decimal("0"))
        else:
            total_without_vehicle_tax = None

        if _has_text(data.get("totalPremium") or data.get("premiumTotal")):
            total_with_vehicle_tax: Optional[Decimal] = _money(data.get("totalPremium") or data.get("premiumTotal"))
        elif total_without_vehicle_tax is not None and vehicle_tax_value is not None:
            total_with_vehicle_tax = total_without_vehicle_tax + vehicle_tax_value
        else:
            total_with_vehicle_tax = total_without_vehicle_tax
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
            "coverage_items": coverage_items,
            "proposal_info": proposal_info,
            "proposal_coverage_items": [
                {
                    **item,
                    "name": PICC_PROPOSAL_KIND_NAME_BY_CODE.get(_to_str(item.get("code")).strip()) or _to_str(item.get("name")).strip(),
                }
                for item in coverage_items
            ],
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
        return {
            "mode": _profile_text(profile, "mode", "picc_motor_real"),
            "status": "quoted",
            "platform_code": "PICC",
            "platform_name": "人保",
            "account_type_name": account_type_name,
            "quotation_no": data.get("quotationNo"),
            "quotation_id": data.get("quotationId"),
            "plate_no": _first_text(data.get("licenseNo"), vehicle.get("licenseNo")),
            "owner_name": owner.get("ownerName"),
            "vehicle_model": vehicle.get("selectedModelName") or vehicle.get("modelName"),
            "vehicle_actual_value": _money_text_or_empty(vehicle.get("actualValue")),
            "joint_sales": tujia_anshun,
            "joint_sales_premium": _money_text(joint_sales_premium) if joint_sales_premium_present else "",
            "joint_sales_amount": _money_text(joint_sales_amount) if joint_sales_amount_present else "",
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
