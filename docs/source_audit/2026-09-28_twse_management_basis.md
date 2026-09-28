# TWSE「管理股票」類別不適用之官方依據

日期：2026-09-28。狀態：**依據已查證，待使用者核准後才可寫入 `config/trading_status_approvals.json` 的 `not_applicable`**。

## 問題

交易狀態政策 `trading-status-policy-2` 的必要類別包含 `management`（管理股票）。TPEx 政府開放資料的變更交易名單有「管理股票」欄，TWSE 沒有任何對應來源，因此 100 檔上市股票在此類別永遠是 `MISSING_COVERAGE` → `unknown`，即使其他來源全部核准，上市股票也無法成為 allowed。

## 官方依據

- 來源：臺灣證券交易所股份有限公司營業細則（修正日期民國 115 年 08 月 06 日），`https://twse-regulation.twse.com.tw/TW/law/DAT0201_print.aspx?FLCODE=FL007304`
- 封存：`artifacts/source-audit/regulation/20260928/TWSE_FL007304_營業細則.html`，抓取時間 2026-09-28T13:27:46+08:00，SHA-256 `2f40ccb048ebea431a68311eeeed84b283cb7decd0596eb53d59be9d28cd9ea2`（`artifacts/` 不納入 Git，雜湊記錄於此）。
- 全文僅第 52 條提及「管理股票」：

  > 第 52 條　經本公司終止上市之有價證券，除另有規定外，本公司應於實施日四十日前公告之，並即通知櫃檯買賣中心及該上市公司得申請為管理股票。……

- 第 49 條規定上市公司符合淨值低於股本二分之一、未如期召開股東常會、會計師出具繼續經營重大不確定性或保留意見等情事時，列為「變更交易方法有價證券」（全額交割）。此類限制已由 TWSE 變更交易名單（TWT85U／政府開放資料集 11760，`TWSE_SPECIAL_OGD`）的 `special_trading` 類別涵蓋。

## 判定

管理股票是**經證交所終止上市後**、由該公司向櫃買中心申請、改在櫃買中心管理交易的股票；仍在證交所上市的股票依定義不會同時是管理股票。上市股票對應的基本面異常限制是第 49 條變更交易方法，已屬 `special_trading`。因此「TWSE × management」可版本化為不適用，理由與證據如上。

## 仍需注意

- 若交易池中的上市股票在比賽期間被終止上市，它會離開證交所，屆時由行情缺漏、終止上市公告與 Guard 另行處理；不因本判定而被視為可交易。
- `trading_halt` 類別目前以 TWSE 暫停交易資料集（11677）對應；證交所另有「停止買賣」制度（營業細則第 50 條起），兩者是否由同一資料集涵蓋仍待 9/29 交易日觀測與文件核對，屬 TS0 其餘待核事項，與本判定無關。

## 核准方式

使用者同意後，於 `config/trading_status_approvals.json` 的 `not_applicable` 加入：`market=TWSE`、`category=management`、`basis`（引用第 52 條）、`evidence`（本文件路徑）、`approved_by`、`approved_at`（含時區）。程式只在此項存在時才為 TWSE 管理股票產生 `not_applicable_policy` coverage。
