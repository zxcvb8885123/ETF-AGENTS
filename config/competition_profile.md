# 競賽設定檔填寫說明

`competition_profile.json` 是 D-Plan 自動整理使用的隊伍設定檔，不是提交給主辦方的 D-Plan。設定確認後，程式會從已驗證的 Decision run 自動建立 `context.json`，再產生 D-Plan v4.3 候選檔。

## 需要人工確認的欄位

在 `competition_profile.json` 填寫下列欄位前，`strategy_status` 必須維持 `pending_user_confirmation`；程式會停止正式輸出。

| JSON 路徑 | 規則 | 說明 |
| --- | --- | --- |
| `team_id` | 主辦方配發 | 本隊為 `TEAM_11067`；不得自行改寫。 |
| `strategy.etf_name` | 1–20 字 | ETF 公開說明書的名稱，須以「主動」開頭。 |
| `strategy.investment_theme` | 1–50 字 | ETF 公開說明書的投資主題。 |
| `strategy.investment_philosophy` | 100–300 字 | ETF 公開說明書的投資理念；每日 D-Plan 必須與其一致。 |
| `strategy.approved_at` | `YYYY-MM-DDTHH:MM:SS+08:00` | 團隊確認上述策略的時間。 |
| `agent.model_provider` | D-Plan 列舉值 | 實際決策模型供應商，例如 Codex 使用 OpenAI 時填 `openai`。 |
| `agent.model_version` | 非空文字 | 實際使用的模型版本；不可把未確認的 CLI 版本冒充模型版本。 |

全部確認後，將：

```json
"strategy_status": "pending_user_confirmation"
```

改成：

```json
"strategy_status": "approved"
```

## 自動產檔

完成設定後，使用已通過的 `approved` 或 `no_trade` Decision run 執行：

```bash
docker compose run --rm agent python3 cli/dplan.py build \
  --trade-date YYYY-MM-DD \
  --daily-run artifacts/daily_runs/DAILY_RUN_ID/pipeline.json \
  --decision-repository artifacts/portfolio_decisions \
  --decision-run-id DECISION_RUN_ID \
  --virtual-account-account-id ai-cup-2026 \
  --virtual-account-run-id PREPARE_RUN_ID
```

程式會自動產生：

```text
artifacts/competition/contexts/context_DECISION_RUN_ID.json
artifacts/competition/D-Plan_TEAM_11067_YYYY-MM-DD.json
```

`context.json` 與 D-Plan 都只會使用封存決策內既有的證據、動能、交易意圖、現金姿態與帳本資料；缺少來源、策略尚未確認、帳本不一致或風控拒絕時，一律停止，不自行補值。

> 若此專案供不同隊伍共用，請不要共用含真實隊伍資料的 `competition_profile.json`；請各隊建立自己的 profile，並以 `--profile` 指定路徑。
