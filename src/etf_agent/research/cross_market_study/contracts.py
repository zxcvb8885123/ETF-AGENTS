"""跨市場隔夜訊號回測的契約與事先固定的判準。

離線研究，不屬於決策鏈：``decision/``、``automation/`` 不得匯入本套件（有測試鎖定）。
判準在看到任何結果前寫死在這裡，結果不得回頭調整。"""
from decimal import Decimal


class CrossMarketStudyError(ValueError):
    """回測輸入、資料或統計條件不符。"""


SCHEMA_VERSION = "cross-market-study-1.0"
DATA_SCHEMA_VERSION = "cross-market-data-1.0"

# 取多少日曆日的歷史：兩年，才夠 250 個交易日的最低樣本門檻。
DEFAULT_HISTORY_DAYS = 760
# 決策時點：台北 07:00（與每日早上 run 相同）。
DECISION_HOUR, DECISION_MINUTE = 7, 0

YAHOO_SYMBOLS = ("^GSPC", "^IXIC", "^SOX", "EWT", "TSM", "^N225", "^KS11", "^VIX")
TARGET_INDEX = "^TWII"
NIGHT_FUTURES_ID = "TX"

# 預先登記的判準：
MIN_DAYS = 250                      # 樣本少於此只報筆數，不報任何通過與否
PRIMARY_TARGET = "gap"              # 主要檢定只看「開盤跳空」；其餘目標只作探索，不判通過
PRIMARY_SIGNALS = ("us_main", "night", "agreement_score")
PASS_T_STAT = Decimal("2.4")        # 三個主要檢定的 Bonferroni 近似（0.05/3 雙尾）
MIN_GROUP_DAYS = 20                 # 分組平均至少要的天數，不足不報平均
EXPLORATORY_TARGETS = ("open_to_close", "close_to_close", "avg_price", "range")
SIGNALS = ("us_main", "proxy", "asia", "night", "agreement_score", "vix_change")
# 第一輪結果出來後才補的「決策相關」目標（事後加入，報告標 post_hoc）：決策在 t 日成交均價成交，
# 之後賺到的是 t 日均價到 t 日收盤、到 t+1 日均價的報酬；隔夜訊號已反映在 t 日均價裡，能不能再預測這段才是現金姿態的價值。
FORWARD_TARGETS = ("fill_to_close", "fill_to_next_avg")

# 現金姿態模擬（事先登記，不調參）：每日在 t 日均價依姿態重配到目標曝險，報酬是 t 日均價到 t+1 日均價的交易池等權報酬。
CASH_BY_STANCE = {"aggressive": Decimal("0.03"), "neutral": Decimal("0.10"), "defensive": Decimal("0.20")}  # 同 decision_policy.json
BUY_COST = Decimal("0.001425")   # 買進手續費
SELL_COST = Decimal("0.004425")  # 賣出手續費加證交稅
# 規則：訊號 → 姿態；沒列到的日子一律 neutral。基準為「全程 neutral」。
CASH_RULES = {
    "R1_outlook_negative": {"negative": "defensive"},
    "R2_aligned_down": {"aligned_down": "defensive"},
    "R3_both_directions": {"negative": "defensive", "positive": "aggressive"},
}
