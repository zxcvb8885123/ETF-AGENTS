"""市場背景查核的來源與錯誤型別。"""


class MarketProbeError(ValueError):
    pass


SOURCES = {
    "TWSE_FMTQIK": "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK",
    "TWSE_MI_INDEX": "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX",
    "TWSE_BFI82U": "https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={date}&response=json",
}
