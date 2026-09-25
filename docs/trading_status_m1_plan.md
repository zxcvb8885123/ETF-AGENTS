# M1 下一批計畫：官方交易狀態與決策可交易性

狀態：TS1～TS4 已完成契約／重建器／SQLite migration／CLI／Guard adapter 的 fixture 驗收；TS0 官方端點核准與 TS5 150 檔真實覆蓋尚未完成。接續官方歷史行情與財報彙總，先補來源重測與真實保存，再收尾 M1 細粒度工具、進入 M2／M3。

2026-09-25 進度：已取得四份 TPEx 與四份 TWSE 政府開放 CSV，與同日官方 OpenAPI JSON 完成指定日期對應欄位比對；TPEx 注意、處置兩組的整體日期範圍不同。[TS0 來源核准行動計劃](source_audit/2026-09-25_ts0_approval_plan.md)列明各來源剩餘的授權、完整性、更新時點與 TS5 關口；目前核准來源仍為 0。

同日政策修正：`trading-status-policy-2` 的預設必要類別移除 `attention`。注意資訊依[證交所注意／處置作業要點](https://twse-regulation.twse.com.tw/TW/law/DAT0201_print.aspx?FLCODE=FL007225)與本計劃的提示定位，只在來源已核准且資料完整時加入提示與證據；缺少注意來源本身不使交易許可變成 `unknown`。若明確版本化政策把注意列為必要類別，請求仍可指定；舊 `trading-status-policy-1` 的預設類別保留。停牌、變更交易、分盤、管理及處置的必要覆蓋與 fail-closed 不變。

已新增八份政府開放 CSV 的日期精度候選事實正規化：保留公告日與暫定處置區間、停復牌事件日期／時間、原始列雜湊及抓取時間；五、六碼非股票證券不混入 150 檔股票交易池。候選事實沒有完整現況或正式生效時間證據，不能轉成 `allowed`、`blocked` 或正式來源核准。

## 1. 目標與範圍

在固定 `decision_cutoff` 下，回答官方交易池每檔股票於指定交易時段的已知交易狀態，交付可重建的資料包與確定性決策閘門。這是 Data Agent 與 Portfolio Guard 的能力擴充，不新增推理 Agent。

涵蓋 TWSE／TPEx 停止及恢復交易、變更交易、分盤、管理、注意與處置資料。實際類別名稱、適用市場、有效期間與交易限制均須在 TS0 依官方文件核實；此清單不代表所有市場都有同名制度或已有可用端點。

不包含交易執行、平台送件、帳戶接入、完整公司行動帳務、完整歷史回補或無人值守排程。狀態資料不構成基本面結論，不直接產生買賣方向。

## 2. 現有基礎與必須修正的契約

- 已有官方交易池、來源健康探測、原始回應保存、不可變 Snapshot 與 Portfolio Guard。
- `docs/source_feasibility_2026-09-17.md` 記錄 TPEx 部分狀態端點曾可取得資料；這只是舊探測證據，本批仍需重測來源、欄位、授權與完整性。
- `ResearchSnapshot.tradable_symbols`／`not_tradable_symbols` 目前是數量；`decision/risk.py` 卻使用同名欄位作股票集合。不得把計數默默轉成名單，也不得靠「有行情」推定交易許可。
- 既有交易池驗證主要證明名單與行情代號對應，不足以證明指定時段不存在交易限制。

TS1 固定新版決策輸入契約：新增成組的 `TradingStatusBundle` 與 `TradabilityAssessment`，由 adapter 明確轉接；原 Snapshot 計數保留其原義。新 Guard 使用逐檔 assessment，移除對同名計數欄位的集合假設。舊 fixture 保留明確版本與測試入口，不能自動升級成正式資料。

研究 Snapshot 可用性與決策可交易性分開：交易狀態不明時，既有有效財報／事件仍可供研究，但正式交易提案必須被擋下。不能直接改寫已封存 Snapshot 的 `usable` 或既有研究內容雜湊。

## 3. TS0：來源重測與核准

建立兩市場 × 必要類別的來源矩陣，逐項保存：

- 官方文件與 endpoint、查詢參數、回應 schema、更新週期及來源設定版本。
- 使用與保存條件、核准狀態、探測時間、HTTP 結果、樣本雜湊及原始回應 ID。
- 端點是完整現況名單、增量異動或公告歷史；日期欄位是公告日、生效日、截止日或資料產製日。
- 分頁、筆數、查詢涵蓋區間、空回應語意、取消／更正機制與缺漏偵測方式。

只採用核准來源。端點回傳空陣列不直接等於「沒有受限制股票」；必須證明查詢成功、日期及範圍正確、分頁完整。只提供增量事件的來源若缺基準狀態，不能推導完整現況。

舊探測未覆蓋的 TWSE 類別先列缺口；不以模型知識、新聞摘要或 TPEx 狀態代替。每次網路呼叫最多三次嘗試，格式與語意錯誤不靠重試掩蓋。

2026-09-22 重新檢查 TPEx 官方 OpenAPI 目錄，確認目錄列出 `tpex_spendi_today`（暫停／恢復）、`tpex_cmode`（變更交易／分盤／管理／停止）、`tpex_trading_warning_information`（注意）與 `tpex_disposal_information`（處置）等候選端點；這只證明端點在官方目錄存在，尚未證明欄位、查詢日期、空回應及歷史可得時間語意，因此目前仍列為 `candidate`，沒有寫入 approved allowlist。

## 4. 資料契約草案

以下名稱與欄位在 TS1 固定正式 schema，現已由 `src/etf_agent/data/trading_status.py` 提供確定性實作；官方 Provider 核准仍是 TS0／TS5 缺口。

| 物件 | 必要內容 |
| --- | --- |
| `TradingStatusRequest` | request ID、官方交易池版本／雜湊、cutoff、帶時區的 target session 起訖、必要市場／類別、來源設定與政策版本 |
| `TradingStatusRecord` | symbol、市場、類別、原始狀態碼、有效起訖、原始日期與時間精度、取消／承接紀錄 ID、內容版本、evidence ID |
| `TradingStatusSourceCoverage` | source、核准狀態、完整／增量語意、as-of、查詢區間、分頁／筆數、freshness、raw ID／雜湊、來源成功或缺漏原因 |
| `TradingStatusBundle` | schema、bundle ID、snapshot ID／雜湊、cutoff、target session、請求雜湊、全部狀態／來源證據、固定覆蓋分母、缺口與內容雜湊 |
| `TradabilityAssessment` | bundle／政策／規則版本雜湊、逐檔官方狀態旗標、allowed／blocked／unknown、適用限制、reason codes、evidence IDs、覆蓋與整批狀態 |

每筆來源證據保留 URL、`published_at`（無法證明時 null）、`available_at`、`fetched_at`、內容時間、原始回應 ID／雜湊、parser 版本。symbol 與市場必須符合官方名單；代號承接需要獨立官方證據，不自行合併。

覆蓋分母固定為請求交易池 × 各市場所需類別。非適用類別須有版本化政策及官方依據，不得由 LLM 臨時移出分母。來源成功不代表狀態允許交易：已確認停牌可以是完整資料覆蓋，同時 assessment 為 blocked。

## 5. 時間點與狀態重建

- 所有判斷使用 `available_at <= decision_cutoff` 的版本；抓取發生在 cutoff 之後就不能放入該次決策。
- 生效時間與可得時間分開。cutoff 前公告、未來交易時段才生效的限制，應作用在指定 target session；不能只看目前是否已生效。
- 保存日期精度；只有日期時，由核准政策與交易時段資料推導區間，保留推導依據，不偽造精確公告時間。缺時段或邊界語意時回傳 unknown。
- 停止／恢復／取消／更正依事件與有效區間重建，不能單純以最後抓到的一列覆蓋。有效期間衝突且無官方解釋時 unknown。
- 恢復交易只解除對應的停止狀態；其他限制仍獨立判斷。過期旗標不得繼續套用，但只有在有效期終點可證明時才能解除。
- 缺少股票列只能在完整、適時且成功的必要來源覆蓋下推論未列限制，並引用涵蓋證據。HTTP 失敗、過時資料或未完成分頁皆為 unknown。
- freshness 門檻依來源更新週期與 target session 設定並版本化，不一律用固定天數，也不以系統當日取代歷史 cutoff。
- 未有版本化交易日曆前，僅可使用明確且有來源的單一目標時段；不得宣稱自動推導下個交易日或具備正式歷史重播能力。

## 6. 確定性政策與 Guard

以下是擬定的系統保守政策，不是對所有官方制度的法律或交易規則判定：

| 已驗證資料狀態 | 系統處理 |
| --- | --- |
| target session 內停止交易 | blocked，拒絕在該時段建立交易訂單 |
| 有有效恢復證據 | 重新核對全部其他必要類別，再決定 allowed／blocked／unknown |
| 注意旗標 | 單獨保留提示，不自動等同停止交易；仍核對其他狀態 |
| 分盤、處置、變更交易或管理等限制 | 保留官方限制；首版若無對應成交／委託模型則 blocked，原因明列為系統尚不支援 |
| 缺資料、來源不明、過時或衝突 | unknown，正式提案 fail closed |
| 全部必要來源完整且沒有不支援限制 | allowed；只代表資料及政策閘門通過，不保證成交 |

Guard 需區分訂單與既有持倉：既有停牌持股不因無法交易就被當成可賣出，也不能自動清零或估造成交。禁止新建不允許的買／賣訂單；no_trade 與持有狀態保留限制，另驗現金、曝險、價格品質與 CompetitionGuard。unknown 覆蓋仍阻擋正式決策完成；blocked 並不自動代表目前持倉違反競賽規則。

正式決策 adapter 必須驗證 bundle 與 assessment 成對存在、cutoff／snapshot／交易池／目標時段／政策版本一致，並重建 assessment。所有訂單與情境套用同一版本；Risk 子 Agent 不得將 blocked 或 unknown 改成 allowed。

## 7. 元件、CLI 與保存

核心放在 `src/etf_agent/data/` 的交易狀態 Provider、parser、collector、repository、assessment builder／validator；決策轉接與 Guard 留在 `src/etf_agent/decision/`。SQLite migration 可重跑，不改既有快照或原始回應。

原始回應先保存再解析；解析失敗保留 run 與診斷。同內容重抓去重並保存每次取得紀錄，更正新增版本。Snapshot／bundle 鎖定版本集合；不得在 Validator 重播時查詢「最新狀態」。

CLI 預定在 `cli/data_agent.py` 增加收集與查詢入口，或依既有維運慣例提供薄 scripts wrapper；兩者不得複製核心邏輯。介面涵蓋 collect、status、build-bundle、validate，輸入明列 cutoff 與 target session，stdout 為 JSON、進度走 stderr。

收集／bundle 的退出碼：0＝請求資料完整且驗證通過（允許含已確認 blocked 標的）、2＝有缺口／降級、1＝設定／執行／完整性失敗。決策閘門另外輸出拒絕原因，不把收集成功當作交易允許。

## 8. 開發階段

| 階段 | 交付 | 驗收條件 |
| --- | --- | --- |
| TS0 來源重測 | 官方來源矩陣、樣本、授權與時間語意 | 每個採用來源有核准證據；未確認者列缺口 |
| TS1 契約／政策 | 嚴格 schemas、時間區間、覆蓋與新版決策 adapter 規格 | 計數／股票清單衝突有明確版本處理；缺資料不可升級 allowed |
| TS2 收集／保存 | parser、原始回應、版本紀錄、migration、增量收集 | 更正與去重可追溯；來源失敗不標記整批成功 |
| TS3 Bundle／Validator | 同 Snapshot 的固定狀態包、逐檔 assessment、重建驗證 | future data、衝突與竄改皆被拒絕；限制與 unknown 不混淆 |
| TS4 決策接入／CLI／Skill | Guard 與情境更新、data CLI、event-data Skill、文件 | 真實資料無法繞過狀態閘門；既有持倉及 no_trade 行為明確 |
| TS5 有限實測與回歸 | 官方 150 檔 × 必要類別覆蓋報告、原始證據與驗收紀錄 | 固定分母列完整缺口；資料完整與可交易數分開，全部要求檢查通過 |

本批目標為 TS0～TS5。目前已交付可重建的 JSON Provider 邊界、Parser、Bundle／Assessment Validator、SQLite 保存、CLI 與 Guard adapter；來源無法證明完整性時只輸出降級／unknown。fixture 中的停復牌案例不能冒充當日市場真實事件。

## 9. 測試與交付界線

至少涵蓋：

- 正常狀態、停牌、恢復、其他限制仍有效、日期邊界及未來才生效的已知公告。
- cutoff 後取得、無時區、晚到公告、更正版本、舊 Snapshot 不變。
- 正常空名單、錯誤空回應、分頁截斷、過時來源、增量缺基準與來源衝突。
- 股票誤配、代號承接無證據、重複事件、固定覆蓋分母與不適用類別。
- 限制原因、引用、原始回應、數值／名單／內容雜湊遭修改。
- Guard 接收到舊計數欄位、缺少成對 bundle、錯誤目標時段、缺政策版本。
- 已持有停牌股票、賣單無法執行、no_trade、情境重建與未知狀態 fail-closed。
- CLI JSON／退出碼、有限重試、失敗保存與 migration 重跑。

實作後執行專案規定的完整 unittest、compileall、git diff --check；大幅更新 event-data Skill 時另跑 quick_validate.py。交易狀態可用不代表帳戶、競賽規則、基準、歷史回測或 D-Plan 已完成。本批驗收後接 M1 細粒度工具；基本面 FR4／FR5 仍依其資料與契約前置條件進行。
