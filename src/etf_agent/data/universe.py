import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class Instrument:
    symbol: str
    code: str
    name: str
    market: str
    source_date: str


def normalize_symbol(value: str, market: str = "TWSE") -> str:
    value = value.strip().upper()
    if value.endswith(".TW") or value.endswith(".TWO"):
        return value
    suffix = ".TWO" if market.strip().upper() in {"TPEX", "OTC", "上櫃"} else ".TW"
    return value + suffix


def load_universe(path: Path) -> List[Instrument]:
    instruments: List[Instrument] = []
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"symbol", "name", "market", "source_date"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("交易池 CSV 欄位必須包含：symbol,name,market,source_date")
        for line_number, row in enumerate(reader, start=2):
            market = (row.get("market") or "").strip().upper()
            raw_symbol = (row.get("symbol") or "").strip()
            if not raw_symbol:
                continue
            symbol = normalize_symbol(raw_symbol, market)
            if symbol in seen:
                raise ValueError("交易池第 %d 行有重複股票：%s" % (line_number, symbol))
            seen.add(symbol)
            instruments.append(
                Instrument(
                    symbol=symbol,
                    code=symbol.split(".", 1)[0],
                    name=(row.get("name") or "").strip(),
                    market=market,
                    source_date=(row.get("source_date") or "").strip(),
                )
            )
    return instruments
