"""probe 的 failure_condition 時間條件結構檢查：不誤殺真實模型可能寫出的合理句子、擋掉空泛句子，回饋訊息中性並列兩種修法。"""

import unittest

from etf_agent.decision.time_condition import time_condition_error
import test_offense

REASONABLE = (
    "兩個交易日內交易池再創新低且量能放大，即視為這次試單失敗",
    "若下一個收盤加權指數跌回今日低點之下，代表站回是假突破，試單失敗",
    "買進後 5 天內交易池平均報酬仍低於加權指數，或賣壓竭盡的跡象消失，視為失敗",
    "本週五收盤前，外資沒有轉為買超且廣度回到 40% 以下，則判定反彈不成立",
    "11/18 前交易池上漲家數比例未能維持在一半以上，就算失敗",
    "三日內再度出現放量長黑、且加權指數收盤跌破月線，視為失敗",
    "下週開盤若指數跳空下跌超過今日反彈幅度的一半，視為失敗，轉回保守",
    "隔日收盤若多數持股收在前一日低點之下，失敗",
)
VAGUE = (
    "",
    "   ",
    "失敗",
    "指數表現不好時就算失敗了",
    "短期內若市場轉弱就算失敗",
    "一段時間後仍未反彈視為失敗",
    "日後如果情況惡化再檢討",
    "之後若指數跌破支撐則失敗",
    "市場情緒變差、賣壓再起的時候",
)


class FailureConditionStructureTests(unittest.TestCase):
    def test_reasonable_traditional_chinese_conditions_pass(self):
        for text in REASONABLE:
            self.assertIsNone(time_condition_error(text), text)

    def test_vague_sentences_are_rejected(self):
        for text in VAGUE:
            self.assertIsNotNone(time_condition_error(text), repr(text))

    def test_feedback_is_neutral_and_lists_both_fixes(self):
        message = time_condition_error("指數表現不好時就算失敗了")
        self.assertIn("補上明確期限或時間點", message)  # 補依據
        self.assertIn("改選較保守的等級", message)  # 降級
        for blame in ("錯誤", "不得", "禁止", "必須"):
            self.assertNotIn(blame, message)
        self.assertIn("改選較保守的等級", time_condition_error("失敗"))


class FailureConditionInStanceTests(unittest.TestCase):
    """沿用 test_offense.StanceTests 的夾具，只測 failure_condition 在整份姿態驗證裡的行為。"""

    def setUp(self):
        self.fixture = test_offense.StanceTests("test_valid_probe")
        self.fixture.setUp()

    def run_stance(self, **fields):
        return self.fixture.run_stance(**fields)

    def test_reasonable_conditions_do_not_fail_the_stance(self):
        for text in REASONABLE:
            self.assertEqual(self.run_stance(failure_condition=text), [], text)

    def test_vague_conditions_fail_closed_with_one_neutral_error(self):
        for text in VAGUE[1:]:
            errors = self.run_stance(failure_condition=text)
            # 一則實際錯誤，加上驗證有錯時固定附的「改等級請清空不適用欄位」中性提醒。
            self.assertEqual(len(errors), 2, (text, errors))
            self.assertIn("failure_condition", errors[0])
            self.assertIn("改選較保守的等級", errors[0])
            self.assertIn("提醒：改等級時請清空", errors[1])

    def test_non_string_condition_is_rejected(self):
        self.assertTrue(self.run_stance(failure_condition=123))


if __name__ == "__main__":
    unittest.main()
