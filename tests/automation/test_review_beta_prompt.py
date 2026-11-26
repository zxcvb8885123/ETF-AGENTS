"""position_review.beta_adjust 的交易 Agent 提示開關：只有 beta_adjust=v1 才加「以殘差判斷個股轉弱」那段，其他逐字不變。"""

import unittest

from etf_agent.automation.team_stages import _REVIEW_BETA_RULES, _review_beta_adjusted


class ReviewBetaPromptTests(unittest.TestCase):
    def test_only_beta_adjusted_reviews_get_the_extra_rule(self):
        self.assertTrue(_review_beta_adjusted({"policy": {"version": "v2", "context": "v1", "beta_adjust": "v1"}}))
        for review in (None, {}, {"policy": {"version": "v2", "context": "v1"}}, {"policy": {"beta_adjust": "v2"}}):
            with self.subTest(review=review):
                self.assertFalse(_review_beta_adjusted(review))
        self.assertIn("residual_since_entry", _REVIEW_BETA_RULES)


if __name__ == "__main__":
    unittest.main()
