import unittest

from etf_agent.automation.batching import BatchMergeError, merge_batch_items, universe_batches, weighted_batches


SYMBOLS = ["%04d.TW" % code for code in range(1101, 1101 + 120)]


class UniverseBatchTests(unittest.TestCase):
    def test_batches_are_sorted_fixed_and_cover_universe(self):
        batches = universe_batches(list(reversed(SYMBOLS)), batch_size=50)
        self.assertEqual([len(batch["symbols"]) for batch in batches], [50, 50, 20])
        self.assertEqual(sum((batch["symbols"] for batch in batches), []), sorted(SYMBOLS))
        self.assertEqual(batches, universe_batches(SYMBOLS, batch_size=50))
        self.assertEqual({batch["batch_count"] for batch in batches}, {3})

    def test_weighted_batches_cap_symbols_and_event_count(self):
        symbols = SYMBOLS[:20]
        weights = {symbol: 5 for symbol in symbols}
        weights[symbols[3]] = 90  # 單檔超過上限自成一批
        batches = weighted_batches(list(reversed(symbols)), weights, max_size=6, max_weight=20)
        self.assertEqual([batch["symbols"] for batch in batches][:3], [symbols[:3], [symbols[3]], symbols[4:8]])
        self.assertTrue(all(len(batch["symbols"]) <= 6 for batch in batches))
        self.assertTrue(all(sum(weights[s] for s in batch["symbols"]) <= 20 for batch in batches if len(batch["symbols"]) > 1))
        self.assertEqual(sum((batch["symbols"] for batch in batches), []), symbols)
        self.assertEqual({batch["batch_count"] for batch in batches}, {len(batches)})
        self.assertEqual(batches, weighted_batches(symbols, weights, max_size=6, max_weight=20))
        # 權重都很小時退回只依檔數切。
        light = weighted_batches(symbols, {}, max_size=6, max_weight=20)
        self.assertEqual([len(batch["symbols"]) for batch in light], [6, 6, 6, 2])
        with self.assertRaises(ValueError):
            weighted_batches(symbols + [symbols[0]], weights, max_size=6, max_weight=20)

    def test_duplicate_universe_symbols_are_rejected(self):
        with self.assertRaises(ValueError):
            universe_batches(SYMBOLS + [SYMBOLS[0]])

    def test_merge_requires_each_batch_to_cover_exactly_its_symbols(self):
        batches = universe_batches(SYMBOLS, batch_size=50)
        outputs = [[{"symbol": symbol, "outlook": "neutral"} for symbol in reversed(batch["symbols"])] for batch in batches]
        merged = merge_batch_items(batches, outputs, "technical")
        self.assertEqual([item["symbol"] for item in merged], sorted(SYMBOLS))

        missing = [list(items) for items in outputs]
        missing[1] = missing[1][1:]
        with self.assertRaisesRegex(BatchMergeError, "未覆蓋"):
            merge_batch_items(batches, missing, "technical")
        foreign = [list(items) for items in outputs]
        foreign[0].append({"symbol": batches[2]["symbols"][0]})
        with self.assertRaisesRegex(BatchMergeError, "非本批"):
            merge_batch_items(batches, foreign, "technical")
        duplicate = [list(items) for items in outputs]
        duplicate[0].append(dict(duplicate[0][0]))
        with self.assertRaisesRegex(BatchMergeError, "重複"):
            merge_batch_items(batches, duplicate, "technical")


if __name__ == "__main__":
    unittest.main()
