# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.quote_assistant_service import (
    _extract_quote_repair_code_command,
    _extract_transfer_vehicle_command,
    detect_quote_config_override_signal,
    detect_quote_data_override_signal,
    detect_quote_signal,
    extract_quote_fields,
)


def main() -> None:
    quote_commands = [
        "人保报价",
        "全保",
        "人保全保",
        "人保交三",
        "交三",
        "人保单商",
        "单商",
        "续保",
        "人保续保",
        "续保交三",
        "续保单商",
        "太平洋报价",
    ]
    for command in quote_commands:
        parsed = detect_quote_signal(command)
        assert parsed.get("is_quote"), (command, parsed)

    config_commands = ["车损改3万", "非车0", "司乘3万", "司乘改3万", "三者200万"]
    for command in config_commands:
        parsed = detect_quote_config_override_signal(command)
        assert parsed.get("is_override"), (command, parsed)
    repair_command = _extract_quote_repair_code_command("送修码3604731000027-濂溪区金鑫汽车修理厂")
    assert repair_command.get("enabled") is True and repair_command.get("query"), repair_command

    data_commands = ["初登日期2024-01-01", "初登改2024-01-01", "号牌种类52", "车架号LSJEM4O92TKO37865"]
    for command in data_commands:
        parsed = detect_quote_data_override_signal(command)
        assert parsed.get("is_override"), (command, parsed)
    transfer_command = _extract_transfer_vehicle_command("非过户车")
    assert transfer_command.get("is_transfer_vehicle") is False, transfer_command

    fields = extract_quote_fields("车主 张三 手机 13900000001 车架号 LSJEM4O92TKO37865")
    assert fields.get("vin") == "LSJEM4092TK037865", fields
    print("PASS command parser: quote/config/data override commands parsed")


if __name__ == "__main__":
    main()
