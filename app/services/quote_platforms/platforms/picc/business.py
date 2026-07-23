# encoding: utf-8
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import date, datetime, timedelta
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
QUERY_QUALITY_FLAG_PATH = "/khyx/newFront/qth/price/queryQualityFlag.do"
GET_CLUB_GIFT_DISPLAY_INFO_PATH = "/khyx/newFront/qth/price/getClubGiftDisplayInfo.do"
QUERY_CAR_CHECKER_PATH = "/khyx/newFront/common/queryCarchecker.do"
QUOTE_PATH = "/khyx/newFront/qth/price/quote.do"
QUERY_QUOTE_TIMES_PATH = "/khyx/newFront/qth/price/queryQuoteTimes.do"
CLEAR_JS_QUOTATION_NO_PATH = "/khyx/newFront/qth/price/clearJSQuotationNo.do"

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

PRODUCT_FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    PRODUCT_COMPULSORY: ("交强", "交强险"),
    PRODUCT_LOSS: ("机动车损失保险", "车损险", "车辆损失险", "车损"),
    PRODUCT_THIRD_PARTY: ("第三者责任险", "第三责任险", "三者险", "三者", "三责"),
    PRODUCT_DRIVER: ("车上人员责任险（司机）", "车上人员责任险(司机)", "司机险", "司机责任险", "司机"),
    PRODUCT_PASSENGER: ("车上人员责任险（乘客）", "车上人员责任险(乘客)", "乘客险", "乘客责任险", "乘客"),
    PRODUCT_SHARED_LIMIT: ("共享主险限额", "主险限额共享"),
    PRODUCT_MEDICAL_THIRD: ("医保外医疗费用责任险（第三者责任险）", "医保外医疗费用责任险(第三者责任险)", "医保外三者", "医保外"),
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
    "验车人工号": "A360400761",
    "验车人姓名": "王飞武",
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
    ) -> None:
        super().__init__(message)
        self.action = action
        self.platform_response = platform_response
        self.request_body = dict(request_body or {})


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


def _first_text(*values: Any) -> str:
    for value in values:
        text = _to_str(value).strip()
        if text:
            return text
    return ""


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


def _clean_money_text(value: Any, default: str = "0") -> str:
    amount = _money(value, default)
    if amount == amount.to_integral():
        return str(int(amount))
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    text = _to_str(message).strip()
    text = re.sub(r"^[A-Z]{2,}[A-Z0-9]*\d*(?:-\d+)?", "", text).lstrip("-_：:，, ")
    return text


def _platform_status_code(data: Any) -> int:
    payload = _json_obj(data)
    try:
        return int(payload.get("status") or 0)
    except Exception:
        return 0


def _platform_message(data: Any, default: str = "平台返回业务校验失败") -> str:
    payload = _json_obj(data)

    def candidates(value: Any, *, depth: int = 0):
        if depth > 4:
            return
        if isinstance(value, Mapping):
            for key in (
                "errorMessage",
                "businessControlMsg",
                "businessMsg",
                "message",
                "msg",
                "errorMsg",
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
        if low in {"success", "ok", "fail", "failed", "error"}:
            continue
        return text
    return default


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
    return {
        "status": payload.get("status"),
        "statusText": payload.get("statusText"),
        "message": _platform_message(payload, ""),
        "response": _compact_platform_payload(payload),
    }


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
    if text in {"0", "false", "no", "n", "否", "不", "不选", "关闭"}:
        return False
    if text in {"1", "true", "yes", "y", "是", "选", "勾选", "开启"}:
        return True
    return default


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


def _is_no_data_platform_response(data: Any) -> bool:
    raw = _to_str(data)
    return bool(re.search(r"(无数据返回|无数据|未查询到|没有查询到|暂无数据|没有数据)", raw))


def _pick_highest_price_vehicle(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=_vehicle_price)


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


def _vehicle_model_suffix_from_type(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value))
    for suffix in ("轿车", "客车", "货车", "越野车", "牵引车", "专项作业车", "摩托车", "挂车"):
        if suffix in text:
            return suffix
    return ""


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


def _used_fuel_model_query_terms(model_name: Any, vehicle_type: Any = "", energy_model_suffix: Any = "") -> List[str]:
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
    return _dedupe_model_terms([energy_typed, typed, no_brand_suffix, raw])


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
            runtime_stage = "build_stub_result"
            quote_result = self._build_stub_quote_result(ctx, quote_payload, request_body)
            if is_real_quote:
                runtime_stage = "prepare_quote"
                request_body = self._prepare_used_fuel_quote(client, ctx, quote_payload, account_type_name=real_account_type)
                runtime_stage = "submit_quote"
                quote_response = self._submit_used_fuel_quote(client, request_body)
                runtime_stage = "build_quote_result"
                quote_result = self._build_used_fuel_quote_result_from_response(ctx, quote_payload, request_body, quote_response)
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
                message="PICC 报价完成" if is_real_quote else "PICC 报价链路已通过协议会话跑通，当前返回本地假报价结果",
                data=success_data(
                    client,
                    extra={
                        "mode": quote_result.get("mode") or "picc_protocol_stub",
                        "request_body": request_body,
                        "quote_result": quote_result,
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
                data_payload["request_body"] = getattr(exc, "request_body", None) or data_payload["request_body"]
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
            "quoteForm": quote_form,
            "defaultFields": defaults,
            "preflight": {
                "vehicleSearch": {
                    "candidateCount": None,
                    "selectedBy": "pending_online_vehicle_query",
                    "selectedPrice": vehicle.get("purchasePrice"),
                    "reason": "当前为离线草稿，登录恢复后会重新查询车型列表并选择购置价最高车型",
                },
                "preciseVehicle": precise_vehicle,
                "actualValue": {"offline": True, "value": _money_text(actual_value)},
                "selectedVehicle": selected,
                "quoteFormError": quote_form_error,
                "missingDefaultConfig": missing_config,
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
        vehicle = self._base_used_fuel_vehicle(defaults, normalized_data, profile=profile)
        owner = self._used_fuel_owner(defaults, normalized_data)

        search_result = self._query_vehicle_candidates(client, vehicle)
        candidates = _vehicle_rows(search_result)
        selected = _pick_highest_price_vehicle(candidates)
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
        precise_vehicle = precise_rows[0] if precise_rows else {}
        actual_value_result = self._query_actual_value(client, vehicle, defaults, selected, precise_vehicle, profile=profile)
        actual_value = _actual_value_from_response(actual_value_result, selected.get("actualValue"))

        selected_price = _money(_first_text(selected.get("purchasePrice"), selected.get("priceP"), selected.get("priceT")))
        search_code = _model_search_code(_first_text(selected.get("VEHICLE_FGW_CODE"), selected.get("vehicleFgwCode"), vehicle.get("modelName")))
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
                "modelCode": _first_text(precise_vehicle.get("vehicleId"), selected.get("vehicleId"), selected.get("modelCode")),
                "platModelCode": _first_text(precise_vehicle.get("platModelCode"), selected.get("platModelCode")),
                "selectedModelName": _first_text(precise_vehicle.get("vehicleName"), selected.get("vehicleName"), vehicle.get("modelName")),
                "selectedVehicleAlias": _first_text(selected.get("vehicleAlias"), precise_vehicle.get("vehicleAlias")),
                "selectedVehicleId": _first_text(precise_vehicle.get("vehicleId"), selected.get("vehicleId")),
                "vehicleFgwCode": search_code,
            }
        )
        checker_info = self._query_car_checker(client, defaults)
        if checker_info:
            vehicle["carchecker"] = _first_text(checker_info.get("userName"), vehicle.get("carchecker"))
            vehicle["mainComCode"] = _first_text(checker_info.get("comCode"), vehicle.get("mainComCode"))
        products = self._used_fuel_products(defaults, profile=profile, actual_value=actual_value, seat_count=vehicle.get("seatCount"))
        quote_form = self._build_used_fuel_quote_form(defaults, vehicle, owner, selected, precise_vehicle, products, profile=profile)
        prechecks = self._run_used_fuel_quote_prechecks(client, defaults, quote_form)

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
            "quoteForm": quote_form,
            "defaultFields": defaults,
            "preflight": {
                "vehicleSearch": {
                    "candidateCount": len(candidates),
                    "selectedBy": "highest_purchase_price",
                    "selectedPrice": vehicle.get("purchasePrice"),
                },
                "preciseVehicle": precise_vehicle,
                "actualValue": actual_value_result,
                "taxabate": taxabate_result,
                "carChecker": checker_info,
                "quotePrechecks": prechecks,
            },
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
        monopoly_code = _to_str(_field_value(defaults, "专管代码", "monopolyCode")).strip()
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
            out["duplicateVin"] = {"total": duplicate_payload.get("total", 0)}
            if _safe_int_local(duplicate_payload.get("total"), 0) > 0:
                out["duplicateVin"]["warning"] = "平台返回已有历史记录，本次继续提交并以最终报价接口结果为准"
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
        main_com_code = _first_text(vehicle.get("mainComCode"), _field_value(defaults, "归属机构代码", "机构代码", "comCode"))
        query_area = _first_text(_field_value(defaults, "查询区域代码", "queryArea"), (main_com_code[:2] + "0000") if len(main_com_code) >= 2 else "")
        model_code = _first_text(precise_vehicle.get("vehicleId"), vehicle.get("modelCode"), selected.get("vehicleId"), selected.get("modelCode"))
        vehicle_model_code = _first_text(precise_vehicle.get("platModelCode"), selected.get("vehicleModelCode"), vehicle.get("platModelCode"))
        purchase_price = _clean_money_text(vehicle.get("purchasePrice"), "0")
        actual_value = _money_text(vehicle.get("actualValue"))
        brand_id = _first_text(selected.get("brandId"), precise_vehicle.get("brandId"))
        vehicle_fgw_code = _first_text(
            vehicle.get("vehicleFgwCode"),
            selected.get("vehicleFgwCode"),
            precise_vehicle.get("VEHICLE_FGW_CODE"),
        )
        search_code = _first_text(selected.get("searchCode"))
        if not search_code:
            search_code = f"{brand_id}-{vehicle_fgw_code}" if brand_id and vehicle_fgw_code else vehicle_fgw_code
        ton_count = _first_text(precise_vehicle.get("tonCount"), selected.get("tonCount"), selected.get("vehicleTonnage"))
        if _money(ton_count) == 0:
            ton_count = ""

        compulsory_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_COMPULSORY, "20"), "20")
        loss_amount = _money_text(_first_text(_profile_product_default(defaults, prof, PRODUCT_LOSS), actual_value))
        third_party_amount = _wan_or_amount_to_wan_text(_profile_product_default(defaults, prof, PRODUCT_THIRD_PARTY, "300"), "300")
        driver_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_DRIVER, "2"), "2")
        passenger_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_PASSENGER, "2"), "2")
        medical_third_amount = _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_MEDICAL_THIRD, "300"), "300")

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
            "renewed": "0",
            "groupCodeValidStatus": "0",
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
            "prpCitemCar.brandName": _first_text(vehicle.get("modelName"), selected.get("vehicleName")),
            "prpCitemCar.brandId": brand_id,
            "prpCitemCar.brandIDNew": _first_text(selected.get("brandIDNew"), f"{brand_id}0" if brand_id else ""),
            "prpCitemCar.vehicleMaker": _first_text(selected.get("vehicleMaker"), precise_vehicle.get("vehicleMaker")),
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
        product_rows = [
            (0, compulsory_amount, "051074", PRODUCT_COMPULSORY),
            (1, loss_amount, "051050", PRODUCT_LOSS),
            (2, third_party_amount, "051051", PRODUCT_THIRD_PARTY),
            (3, driver_amount, "051052", PRODUCT_DRIVER),
            (4, passenger_amount, "051053", PRODUCT_PASSENGER),
            (5, medical_third_amount, "051063", PRODUCT_MEDICAL_THIRD),
        ]
        for index, amount, kind_code, kind_name in product_rows:
            form[f"prpCitemKindVos[{index}].amount"] = amount
            form[f"prpCitemKindVos[{index}].kindCode"] = kind_code
            form[f"prpCitemKindVos[{index}].kindName"] = kind_name
            form[f"prpCitemKindVos[{index}].chooseFlag"] = "true"
        form["prpCitemKindVos[5].sharedAmountFlag"] = (
            "1" if _checked(_profile_product_default(defaults, prof, PRODUCT_SHARED_LIMIT, True), default=True) else "0"
        )
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
        engine_no = _first_text(data.get("engine_no"), _field_value(defaults, "发动机号"))
        vin = _first_text(data.get("vin"), _field_value(defaults, "VIN/车架号", "车架号"))
        license_no = _first_text(data.get("plate_no"), _field_value(defaults, "号牌号码"))
        if not license_no and _profile_text(prof, "license_no_strategy") == "new_car_placeholder":
            license_no = _new_car_placeholder_license(engine_no, vin)
        enroll_date = _first_text(
            _date_text(data.get("first_register_date")),
            _date_text(data.get("issue_date")),
            _date_text(_field_value(defaults, "初登日期")),
        )
        if not enroll_date and _profile_text(prof, "enroll_date_fallback") == "today":
            enroll_date = _today_text()
        return {
            "licenseNo": license_no,
            "licenseType": _first_text(_field_value(defaults, "号牌种类"), "02"),
            "engineNo": engine_no,
            "vin": vin,
            "carKindCode": _first_text(_field_value(defaults, "车辆种类"), "A01"),
            "useNatureCode": _first_text(_field_value(defaults, "使用性质细分种类", "使用性质"), "211"),
            "enrollDate": enroll_date,
            "startDateBI": _first_text(_date_text(data.get("commercial_start_date")), _date_text(_field_value(defaults, "商业起保日期")), next_day),
            "startDateCI": _first_text(_date_text(data.get("compulsory_start_date")), _date_text(_field_value(defaults, "交强起保日期")), next_day),
            "modelName": _normalize_used_fuel_model_name(raw_model_name, vehicle_type),
            "rawModelName": raw_model_name,
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
            "purchasePrice": _first_text(selected.get("purchasePrice"), selected.get("priceP"), selected.get("priceT")),
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
        return [
            {"code": "CI", "name": PRODUCT_COMPULSORY, "required": True, "coverage": _to_str(_profile_product_default(defaults, prof, PRODUCT_COMPULSORY, "20"))},
            {"code": "BI050", "name": PRODUCT_LOSS, "required": True, "insuredAmount": _money_text(loss_amount)},
            {"code": "BI051", "name": PRODUCT_THIRD_PARTY, "required": True, "insuredAmount": _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_THIRD_PARTY, "300"), "300")},
            {"code": "BI060", "name": PRODUCT_DRIVER, "required": True, "insuredAmount": driver_amount},
            {"code": "BI061", "name": PRODUCT_PASSENGER, "required": True, "insuredAmount": passenger_amount},
            {"code": "BI_SHARED", "name": PRODUCT_SHARED_LIMIT, "required": True, "checked": _checked(_profile_product_default(defaults, prof, PRODUCT_SHARED_LIMIT, True), default=True)},
            {"code": "BI_MEDICAL_THIRD", "name": PRODUCT_MEDICAL_THIRD, "required": True, "insuredAmount": _wan_or_amount_to_amount(_profile_product_default(defaults, prof, PRODUCT_MEDICAL_THIRD, "300"), "300")},
        ]

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
        item_rows = _json_obj(quote_response).get("itemKindTempList")
        if not isinstance(item_rows, list):
            item_rows = data.get("itemKindTempList")
        if not isinstance(item_rows, list):
            item_rows = []

        coverage_items: List[Dict[str, Any]] = []
        commercial_premium = Decimal("0")
        compulsory_premium = _money(data.get("ciPremium"))
        for row_any in item_rows:
            row = _json_obj(row_any)
            kind_code = _to_str(row.get("kindCode")).strip()
            name = self._display_kind_name(row.get("kindName"))
            premium = _money(row.get("premium"))
            amount = _money(row.get("amount"))
            if kind_code == "051074" or name == "交强险":
                if not compulsory_premium:
                    compulsory_premium = premium
                continue
            commercial_premium += premium
            coverage_items.append(
                {
                    "name": name,
                    "amount": _clean_money_text(amount),
                    "premium": _money_text(premium),
                }
            )

        response_bi_premium = _money(data.get("biPremium"))
        if response_bi_premium:
            commercial_premium = response_bi_premium
        vehicle_tax = _money(
            _first_text(
                data.get("sumPayTax"),
                data.get("thisPayTax"),
                data.get("carShipTaxes"),
            )
        )
        if not vehicle_tax:
            vehicle_tax = _money(data.get("prePayTax")) + _money(data.get("delayPayTax"))
        total = commercial_premium + compulsory_premium + vehicle_tax
        # PICC sumPremium is insurance premium only; the web result card adds car tax on top.
        response_total = _money(data.get("totalPremium") or data.get("premiumTotal"))
        if response_total:
            total = response_total

        risk_score: Any = ""
        picc_score = _to_str(data.get("piccScore")).strip()
        if picc_score:
            risk_score = _safe_int_local(picc_score, 0)
        warning = _strip_platform_error_code(data.get("errorMessage"))

        claim_bi = _safe_int_local(_json_obj(data.get("carQuoteRiskItemBIRsp")).get("claimTimes"), _safe_int_local(data.get("lastDamagedBI"), 0))
        claim_ci = _safe_int_local(_json_obj(data.get("carQuoteRiskItemCIRsp")).get("claimTimes"), _safe_int_local(data.get("lastDamagedCI"), 0))
        result_card = {
            "title": "报价结果",
            "include_tax": True,
            "total_premium": _money_text(total),
            "commercial_premium": _money_text(commercial_premium),
            "compulsory_premium": _money_text(compulsory_premium),
            "vehicle_tax": _money_text(vehicle_tax),
            "vehicle_tax_detail": {
                "current": _money_text(data.get("thisPayTax")),
                "back": _money_text(data.get("prePayTax")),
                "late_fee": _money_text(data.get("delayPayTax")),
            },
            "joint_sales_premium": _money_text(data.get("sumYelPremium")),
            "driver_accident_premium": _money_text(data.get("DDAPremium")),
            "claim_business_count": claim_bi,
            "claim_compulsory_count": claim_ci,
            "risk_score": risk_score,
            "coverage_items": coverage_items,
            "transfer_available": True,
        }
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
            "vehicle_actual_value": _money_text(vehicle.get("actualValue")),
            "price_items": [
                {"name": "商业险", "amount": float(commercial_premium)},
                {"name": "交强险", "amount": float(compulsory_premium)},
                {"name": "车船税", "amount": float(vehicle_tax)},
            ],
            "premium_total": float(total),
            "result_card": result_card,
            "request_body": request_body,
            "platform_warning": warning,
            "raw_summary": {
                "quotationNo": data.get("quotationNo"),
                "quotationId": data.get("quotationId"),
                "status": _json_obj(quote_response).get("status"),
            },
        }

    def _build_stub_quote_result(
        self,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        request_body: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = _json_obj(quote_payload)
        data = _json_obj(payload.get("normalized_data"))
        return {
            "mode": "picc_protocol_stub",
            "status": "quoted",
            "platform_code": "PICC",
            "platform_name": "人保",
            "plate_no": data.get("plate_no"),
            "owner_name": data.get("owner_name") or data.get("id_name"),
            "price_items": [
                {"name": "商业险", "amount": 1888.0},
                {"name": "交强险", "amount": 950.0},
                {"name": "车船税", "amount": 300.0},
            ],
            "premium_total": 3138.0,
            "request_body": request_body,
            "remark": "最终报价接口待接入，当前为协议会话联调结果",
        }

    def _build_used_fuel_quote_result(
        self,
        ctx: PlatformAccountContext,
        quote_payload: Dict[str, Any],
        request_body: Dict[str, Any],
    ) -> Dict[str, Any]:
        account_type_name = _normalize_account_type(request_body.get("accountTypeName") or ctx.account_type_name or USED_FUEL_ACCOUNT_TYPE)
        profile = _motor_quote_profile(account_type_name) or _motor_quote_profile(USED_FUEL_ACCOUNT_TYPE)
        vehicle = _json_obj(request_body.get("vehicleForm"))
        owner = _json_obj(request_body.get("ownerForm"))
        product_form = _json_obj(request_body.get("productForm"))
        products = [row for row in product_form.get("products", []) if isinstance(row, Mapping)]
        loss_amount = _money(next((_json_obj(p).get("insuredAmount") for p in products if _json_obj(p).get("name") == PRODUCT_LOSS), 0))
        third_amount = _money(next((_json_obj(p).get("insuredAmount") for p in products if _json_obj(p).get("name") == PRODUCT_THIRD_PARTY), 0))
        driver_amount = _money(next((_json_obj(p).get("insuredAmount") for p in products if _json_obj(p).get("name") == PRODUCT_DRIVER), 0))
        passenger_amount = _money(next((_json_obj(p).get("insuredAmount") for p in products if _json_obj(p).get("name") == PRODUCT_PASSENGER), 0))
        medical_amount = _money(next((_json_obj(p).get("insuredAmount") for p in products if _json_obj(p).get("name") == PRODUCT_MEDICAL_THIRD), 0))

        loss_premium = (loss_amount * Decimal("0.01815")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        third_premium = (third_amount / Decimal("10000") * Decimal("4.13")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        driver_premium = (driver_amount / Decimal("10000") * Decimal("23.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        passenger_premium = (passenger_amount / Decimal("10000") * Decimal("7.36")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        medical_premium = (medical_amount / Decimal("10000") * Decimal("1.08")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        commercial_premium = loss_premium + third_premium + driver_premium + passenger_premium + medical_premium
        compulsory_premium = Decimal("950.00")
        vehicle_tax = Decimal("300.00")
        total = commercial_premium + compulsory_premium + vehicle_tax

        coverage_items = [
            {"name": PRODUCT_LOSS, "amount": _money_text(loss_amount), "premium": _money_text(loss_premium)},
            {"name": PRODUCT_THIRD_PARTY, "amount": str(int(third_amount)), "premium": _money_text(third_premium)},
            {"name": PRODUCT_DRIVER, "amount": str(int(driver_amount)), "premium": _money_text(driver_premium)},
            {"name": PRODUCT_PASSENGER, "amount": str(int(passenger_amount)), "premium": _money_text(passenger_premium)},
            {"name": PRODUCT_MEDICAL_THIRD, "amount": str(int(medical_amount)), "premium": _money_text(medical_premium)},
        ]
        return {
            "mode": _profile_text(profile, "stub_mode", "picc_motor_preflight_stub"),
            "status": "quoted",
            "platform_code": "PICC",
            "platform_name": "人保",
            "account_type_name": account_type_name,
            "plate_no": vehicle.get("licenseNo"),
            "owner_name": owner.get("ownerName"),
            "vehicle_model": vehicle.get("selectedModelName") or vehicle.get("modelName"),
            "vehicle_actual_value": _money_text(vehicle.get("actualValue")),
            "price_items": [
                {"name": "商业险", "amount": float(commercial_premium)},
                {"name": "交强险", "amount": float(compulsory_premium)},
                {"name": "车船税", "amount": float(vehicle_tax)},
            ],
            "premium_total": float(total),
            "request_body": request_body,
            "result_card": {
                "title": "报价结果",
                "include_tax": True,
                "total_premium": _money_text(total),
                "commercial_premium": _money_text(commercial_premium),
                "compulsory_premium": _money_text(compulsory_premium),
                "vehicle_tax": _money_text(vehicle_tax),
                "vehicle_tax_detail": {
                    "current": "0.00",
                    "back": "0.00",
                    "late_fee": "0.00",
                },
                "joint_sales_premium": "0.00",
                "driver_accident_premium": "0.00",
                "claim_business_count": 0,
                "claim_compulsory_count": 0,
                "risk_score": 62,
                "coverage_items": coverage_items,
                "transfer_available": True,
            },
            "remark": f"最终报价接口待接入，当前为{account_type_name}协议前置接口联调结果",
        }
