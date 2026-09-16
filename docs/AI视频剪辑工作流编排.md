# **AI 影片剪輯自動化工作流編排執行計劃與技術選型報告**

## **系統架構與核心技術選型**

在建構企業級、高可靠性的 AI 影片剪輯自動化系統時，底層技術選型直接決定了系統的擴展性、維護成本以及人機協作的順暢度。為了實現高效、無縫的自動化剪輯流水線，本系統架構圍繞狀態機編排、草稿底層數據操縱、開發偵錯環境以及多模態 AI 演算法等關鍵維度進行了技術選型與整合。

### **1\. 工作流編排引擎：LangGraph**

影片剪輯是一項高度非線性且需要頻繁人機協同的複雜任務。在本工作流中，存在多個需要人工介入校對、美化與音訊調整的關鍵節點。因此，本系統選擇 LangGraph 作為全域工作流與 AI 代理人（Agent）的編排引擎。  
傳統的靜態工作流引擎（如 Apache Airflow 或 Argo Workflows）在面對需要高頻互動、中斷、狀態回溯以及動態決策的場景時，顯得過於剛性且開發成本高昂。LangGraph 作為基於圖結構（Graph-based）的代理人協調框架，具備以下決定性優勢：

* **原生的人機協同（Human-in-the-Loop）中斷與恢復機制**：LangGraph 提供了原生的 interrupt 函數與 Command 控制原語1。在面臨人工排序、BGM 選配等步驟時，系統能夠自動暫停圖的執行，將當前的上下文快照保存至持久化數據庫中，並在人工操作完成後，攜帶用戶的反饋數據精確地從中斷節點繼續向下執行1。  
* **靈活的狀態管理與執行回溯**：剪輯工作流中的步驟 16 要求「若需在剪映介面操作，則停下來通知我」5。LangGraph 能夠將當前的剪輯狀態（包括軌道配置、素材路徑、翻譯狀態等）打包為統一的狀態類（State Class），並通過執行緒識別碼（Thread ID）進行精確的會話隔離1。一旦偵測到需要人工干預的 UI 標記，狀態機可觸發條件中斷（Conditional Interrupt）並持久化當前進度，有效防範因非確定性代碼執行而導致的工作流雪崩4。

### **2\. 剪映草稿操作工具：pyJianYingDraft**

要對剪映的工程文件進行非破壞性的底層讀寫，必須深入理解其本地存儲機制。剪映專業版與 CapCut 的草稿本質上是由一系列 JSON 文件（主體為 draft\_content.json 與元數據 draft\_meta\_info.json）構成的數據庫9。本系統選用基於 Python 的 pyJianYingDraft 作為底層草稿操縱工具6。

* **底層數據解耦與微秒級精確控制**：剪映的草稿格式採用高度扁平化且解耦的架構，時間軸上的軌道片段（Segments）並不直接包含素材屬性，而是持有唯一的 material\_id 指向對應的素材列表12。pyJianYingDraft 完美還原了這一架構模型，支持微秒級（Microseconds）的時間戳精確計算，能夠自動處理繁雜的 UUID 生成、關聯指向與軌道衝突，極大降低了直接手動修改 JSON 導致文件損壞的風險6。  
* **完美的 VIP 特效與模板模式支持**：工作流中的步驟 9、10、11 涉及到剪映 VIP 特效、花字動畫以及貼紙的注入6。pyJianYingDraft 的「模板模式」（Template Mode）允許系統預先載入一個包含 VIP 素材的空白草稿作為模板，提取其專有的資源 ID（resource\_id），並在運行時將這些高階視覺資產無縫複製並應用到新生成的草稿中，成功繞過了剪映閉源生態的限制6。

### **3\. 本地前端預覽與整合偵錯環境：VS Code Edge 插件**

在 AI 自動化影片剪輯過程中，開發人員與剪輯師需要一個實時、輕量且能與程式碼環境高度整合的預覽窗口。本系統採用 VS Code 的 Microsoft Edge DevTools 插件（或 Live Preview 插件）來啟動與展示 OpenStoryline 的 Web 介面16。

* **內嵌式安全渲染與緩存雙向清理**：該插件利用 headless Chromium 架構，在 VS Code 內部開啟一個真正的瀏覽器視窗，支持實時 HTML/CSS 檢查及 front-end JavaScript 逐行偵錯16。最重要的是，每當開發者重新發佈或預覽新生成的影片場景時，該插件會自動觸發緩存清理機制17，確保剪輯師看到的影片預覽和分鏡畫面是最新的渲染結果，杜絕了因瀏覽器強緩存導致的分鏡時序錯亂問題。

### **4\. 基礎演算法與多模態模型**

自動化流程的視覺理解、劇本策劃與音訊合成能力，依托於 Xiaohongshu 的 Super Intelligence 團隊開源的 FireRed 基礎模型矩陣20。

* **FireRed-OpenStoryline**：作為核心的剪輯 Agent 框架，負責將抽象的用戶剪輯意圖，通過 LLM 規劃與精確的工具編排，轉化為具體的分鏡描述與時間軸指令22。  
* **FireRedASR2S**：業界領先的語音識別系統，用於高精度識別原始影片中的中英文旁白，為步驟 8 的字幕批量生成奠定基礎20。  
* **FireRedTTS2**：長文本多說話人對話生成系統，負責在步驟 17 中合成高保真、情感豐富的英文 AI 配音20。  
* **FireRed-Image-Edit**：強大的圖像編輯模型，具備高保真的指令遵循與局部重繪（Inpainting）能力，用於步驟 15 的中文封面無痕去字與英文版排版重建20。

## **剪映草稿架構、加密防護與工程級應對策略**

自剪映 6.0.0 版本起，國內桌面版剪映對本地草稿檔案 draft\_content.json 引入了強加密保護（通常基於 AES 算法，並依賴動態生成的 key 與 iv），這直接導致許多傳統的明文讀寫工具失效5。  
為了在工業級生產環境中建立高可靠性的流水線，本系統設計了專門的「草稿檢測與版本適配模組」。在工作流啟動初期，系統會自動調用底層解密檢測程序（如 capcut decrypt）來研判草稿的安全狀態，並執行相應的降級或代理策略5。

                  \[ 剪映草稿安全檢測與適配決策流 \]  
                                 │  
                 $ capcut decrypt \<project-dir\>  
                                 │  
                    ┌────────────┴────────────┐  
             \[ 檢測結果: 正常 \]        \[ 檢測結果: 加密 \]  
                    │                         │  
             (encrypted: false)         (encrypted: true)  
                    │                         │  
         ┌──────────┴──────────┐     ┌────────┴────────┐  
         │ 國內版 5.9.0 或     │     │ 國內版 6.0.0+   │  
         │ CapCut 國際版 │     │ (JianYing Pro)  │  
         └──────────┬──────────┘     └────────┬────────┘  
                    │                         │  
         \[ 解鎖全通道 API 讀寫 \]               │  
         直接編輯 draft\_content.json          │  
                                              ▼  
                                  \[ 啟用工程級適配策略 \]  
                        ┌─────────────────────┴─────────────────────┐  
             \[ 策略甲：全局版本鎖定 \]                    \[ 策略乙：單向明文寫入渲染 \]  
             · 強制卸載 6.x+，安裝 v5.9.0     · 由 pyJianYingDraft 全新創建  
             · 阻斷剪映聯網升級路徑              · 寫入未加密的 draft\_content.json  
             · 保持 100% 的雙向 JSON 讀寫能力             · 一旦剪映打開並加密，工作流不再二次讀寫

### **1\. 剪映本地草稿核心結構映射**

下表詳細梳理了剪映草稿的核心 JSON 欄位與本自動化工作流各步驟的底層操作映射關係，為開發者提供了精確的欄位修改依據12：

| 欄位名稱 (JSON Path) | 儲存數據類型與作用 | 關聯操作技能 | 對應工作流步驟 |
| :---- | :---- | :---- | :---- |
| canvas\_config | 畫布高寬比及分辨率配置 (如 "9:16" / "16:9")12 | /openstoryline-to-jianying | 步驟 5 (初始導入配置) |
| tracks | 時間軸軌道數組 (按 z-order 層級疊加影片、音訊、字幕)12 | 所有軌道操作技能 | 步驟 5, 8, 9, 11, 13, 17 |
| tracks\[\].segments | 具體剪輯片段 (包含時間區間及對 material\_id 的引用)12 | /jianying-speed-fit-35s | 步驟 7, 13, 16, 17 |
| materials.videos | 影片與靜態圖片素材清單 (區分 "video" 與 "photo")12 | /openstoryline-to-jianying | 步驟 4, 5 (導入原始分鏡素材) |
| materials.texts | 字幕與文本內容 (樣式、字體及 content 欄位中的轉義 JSON)12 | /jianying-add-subtitles | 步驟 8, 10, 16 (寫入與翻譯字幕) |
| materials.transitions | 轉場效果定義 (關聯至影片相鄰 segment 之間)12 | /jianying-inject-fx | 步驟 9 (VIP 轉場注入) |
| materials.video\_effects | 濾鏡與特效清單 (關聯至特效軌道或特定片段)12 | /jianying-inject-fx | 步驟 9 (VIP 特效注入) |
| materials.stickers | 貼紙元數據 (引用 CapCut 雲端貼紙 resource\_id)12 | /jianying-inject-tts-sticker | 步驟 11 (VIP 貼紙添加) |
| materials.speeds | 影片片段播放速度包絡 (常規變速與曲線變速)12 | /jianying-speed-fit-35s | 步驟 7 (時長限制壓縮) |
| materials.audio\_fades | 音訊淡入淡出對象 (淡入/淡出時間，微秒)12 | 本地音量優化腳本 | 步驟 13 (音量與邊界淡出優化) |
| cover\_info | 影片工程的封面縮略圖幀配置5 | /jianying-make-cover | 步驟 14 (封面幀綁定)5 |

## **AI 影片剪輯 17 步工作流編排執行計劃**

基於上述技術選型，下文為影片剪輯自動化流水線設計的 17 步精確執行計劃。每一步均對應了具體的系統底層行為、調用的 MCP 技能、數據流向及預期輸出。

                              \[ 工作流編排全景流程圖 \]  
                                         │  
┌────────────────────────────────────────┴────────────────────────────────────────┐  
│  \[ 階段一：準備與初始化 \]                                                       │  
│  (1) 清理快取 ──► (2) 啟動 MCP/Web ──► (3) VS Code 預覽 ────────┐               │  
└─────────────────────────────────────────────────────────────────┼────────────────┘  
                                                                  ▼  
┌─────────────────────────────────────────────────────────────────────────────────┐  
│  \[ 階段二：分鏡與初始草稿 \]                                                     │  
│  ┌───────────────────────◄──────────────────────────────────────┘               │  
│  ▼                                                                              │  
│  (4) VLM 分鏡切片 ──► (5) 匯入剪映 (產生 JSON) ──► (6) \[ 人工重排 \] (暫停點 I) ─┐│  
└───────────────────────────────────────────────────────────────────────────────┼──┘  
                                                                                ▼  
┌───────────────────────────────────────────────────────────────────────────────┼──┐  
│  \[ 階段三：時序控制與視覺包裝 \]                                               │  
│  ┌────────────────────────────────────────────────────────────────────────────┘  │  
│  ▼                                                                              │  
│  (7) 變速限制 \<=35s ──► (8) 批量字幕 ──► (9) VIP特效/轉場 ──► (10) 花字樣式 ──► (11) 貼紙 ─┐│  
└────────────────────────────────────────────────────────────────────────────────────────┼─┘  
                                                                                         ▼  
┌────────────────────────────────────────────────────────────────────────────────────────┼─┘  
│  \[ 階段四：音效與封面工程 \]                                                            │  
│  ┌─────────────────────────────────────────────────────────────────────────────────────┘  
│  ▼  
│  (12) \[ 人工 BGM \] (暫停點 II) ──► (13) 調整音量/淡入淡出 ──► (14) 產生多維封面 ──────┐  
└───────────────────────────────────────────────────────────────────────────────────────┼──┐  
                                                                                        ▼  
┌───────────────────────────────────────────────────────────────────────────────────────┼──┐  
│  \[ 階段五：本地化與多語言交付 \]                                                       │  
│  ┌────────────────────────────────────────────────────────────────────────────────────┘  
│  ▼  
│  (15) 封面無痕英文本地化 ──► (16) 吉隆坡草稿字幕翻譯 (異常暫停點) ──► (17) 注入英文配音 (無貼紙) ──► \[ 導出交付 \]  
└──────────────────────────────────────────────────────────────────────────────────────────┘

### **1\. 清理舊的緩存文件**

工作流的第一步是為高負載的影片渲染任務騰出足夠的磁碟空間，避免寫入草稿或下載素材時因空間不足而崩潰。

* **執行機制**：調用 /kuaishou-clean-cache 技能29。底層 Python 腳本會加載 缓存与临时文件分析 配置文件9，精確掃描本地的臨時目錄（Temporary Directories）、日誌文件夾（Logs）以及無效的 SQLite 緩存數據庫。  
* **清理解析對照表**：  
  根據不同平台與工具的快取結構，系統採取差異化的清理策略：

| 快取類別與來源 | 預設文件夾路徑 (OS-Specific Paths) | 清理與保留策略 (Retention Policies) | 釋放儲存空間預期 |
| :---- | :---- | :---- | :---- |
| **剪映臨時轉碼與代理緩存** | %LOCALAPPDATA%\\JianyingPro\\User Data\\Cache \[cite: 31\] | 刪除所有 .tmp 臨時轉碼與代理影片，保留高頻調用的靜態字體文件。 | 10 GB \- 50 GB |
| **快手數據分析工具快取** | %APPDATA%\\kuaishou\\data-analyzer\\cache \[cite: 29\] | 清理歷史分析報告緩存、未上傳的臨時分組 JSON。 | 1 GB \- 5 GB |
| **OpenStoryline 媒體庫快取** | .storyline/downloads/pexels \[cite: 32\] | 刪除超過 7 天未被項目引用的 Pexels 歷史高畫質視訊。 | 5 GB \- 15 GB |
| **系統級 Temp 快取** | %USERPROFILE%\\AppData\\Local\\Temp | 刪除已被剪接程序釋放句柄（Handle）的系統緩存。 | 5 GB |

### **2\. 啟動 FireRed-OpenStoryline 服務**

* **執行機制**：調用 /openstoryline-launcher 技能，一鍵開啟後端 MCP 服務與 Web 前端程序11。  
* **底層行為**：工作流引擎執行並發任務。首先拉起 MCP 伺服器，將本地剪接能力封裝為 JSON-RPC 接口11；隨後啟動基於 FastAPI 的 Web 伺服器，將其部署在本地迴環地址。  
  Bash  
  \# 啟動 MCP 服務  
  PYTHONPATH=src python \-m open\_storyline.mcp.server  
  \# 啟動前端客戶端 Web 服務  
  uvicorn agent\_fastapi:app \--host 127.0.0.1 \--port 8005

### **3\. VS Code 預覽環境初始化**

* **執行機制**：系統在 VS Code 內部啟動 Microsoft Edge DevTools 插件（或舊版 Browser Preview / 新版 Live Preview 插件），將其預設啟動網址設置為前端端口 http://127.0.0.1:800516。這使得剪輯師無需跳出代碼編輯環境，即可直接在右側虛擬機或內嵌標籤頁中觀察 OpenStoryline 的交互介面與分鏡渲染進度18。

### **4\. 導入影片並進行智慧分鏡**

* **執行機制**：將用戶指定的 raw 影片素材導入 FireRed-OpenStoryline22。多模態模型（VLM）對影片進行視覺切片（Visual Segmentation）與內容語意理解，自動生成結構化的分鏡列表（Storyboard）22。系統將輸出一個含有場景描述、情感基調、精確起始時間戳（Timecodes）與關鍵影格特徵的 JSON 數據結構22。

### **5\. 生成剪映初始草稿**

* **執行機制**：調用 /openstoryline-to-jianying 技能9。底層程序讀取步驟 4 的分鏡數據，在 Python 中實例化 pyJianYingDraft6。  
* **底層行為**：  
  1. 建立 draft\_content.json，在 materials.videos\[\] 中寫入影片路徑，設置 type 為 "video"12。  
  2. 在 tracks\[\] 中創建基礎影片軌（Video Track 0），並依據分鏡順序，為每個鏡頭創建一個 segment12。  
  3. 將計算好的 source\_timerange（引用原片區間）和 target\_timerange（在時間軸上的區間）寫入 segments，保存草稿至本機草稿資料夾9。

### **6\. 人工干預一：分鏡時序調整（暫停點）**

* **執行機制**：工作流執行至此觸發 LangGraph 的原生中斷信號 interrupt("等待人工調整影片草稿的順序")1。  
* **底層行為**：工作流自動掛起，在持久化層鎖定當前狀態2。控制端彈出 UI 提示或在終端顯示等待，提示剪輯師進入剪映專業版客戶端，手動拖拽時間軸上的分鏡片段、調整其前後順序6。確認調整完畢後，剪輯師在控制端發送恢復信號（Command(resume=True)），重啟工作流1。

### **7\. 影片總時長壓縮（限制 ![][image1] 秒）**

* **執行機制**：調用 /jianying-speed-fit-35s 技能30。  
* **數學模型與變速演算法**： 系統重載已被人工修改後的 draft\_content.json10，遍歷主影片軌道上的所有 segments，累加計算當前總時長 ![][image2]（以微秒為單位）12。 設影片軌道上共有 ![][image3] 個 segments，目標總時長 ![][image4] 微秒。若 ![][image5]，則計算全域縮放因子 ![][image6]：  
  ![][image7]  
  對於每一個影片片段 ![][image8]，系統在 materials.speeds\[\] 陣列中新增一個變速對象，設置其變速比率為 ![][image6]（代表加速播放，例如常規變速調為 1.2 倍速，或配置對應的曲線變速節點）12。接著，將 segment 的 extra\_material\_refs\[\] 指向該速度對象的 UUID，並同步將其 target\_timerange.duration 縮減為：  
  ![][image9]  
  從而確保影片總長度精確符合「不超過 35 秒」的硬性約束12。

### **8\. 全局批量字幕添加**

* **執行機制**：調用 /jianying-add-subtitles 技能13。  
* **底層行為**：工作流解析 ASR 產出的時間戳文本20，將每句字幕封裝進 materials.texts\[\] 陣列中12。在 tracks 內新開闢一條文本軌道（Layer 1），創建對應的文字 segments12。文字的詳細格式（如字體、初始位置、對齊方式）將被編譯為 JSON-in-JSON 的轉義字串寫入 content 欄位中12。

### **9\. 注入 VIP 轉場與影片特效**

* **執行機制**：調用 /jianying-inject-fx 技能13。  
* **底層行為**：為了在程式碼層面加入 VIP 特效而不觸發剪映的付費阻斷，工作流採用「模板複製注入」技術6。系統從 VIP 特效庫中檢索預置的 effect\_id 與 resource\_id（例如：轉場「疊化/閃黑」、特效「復古膠片/動態抖動」）5。  
  * **轉場寫入**：在 materials.transitions\[\] 中插入轉場對象12，並在相鄰影片 segments 的 extra\_material\_refs 中加入該轉場 UUID，設定其 duration 為 0.5s12。  
  * **特效寫入**：在 materials.video\_effects\[\] 中插入特效配置12，並在 tracks 中新建一條特效軌道（Effect Track），使特效片段在指定的時間段內覆蓋影片層12。

### **10\. 字幕花字與入場動畫自定義**

* **執行机制**：調用 /jianying-inject-text-fx 技能。  
* **底層行為**：此步驟專注於對步驟 8 建立的文字 segments 進行樣式重塑。系統對 materials.texts\[\].content 中的轉義 JSON 進行解包與修改，重置以下視覺參數12：  
  * **花字效果（Fancy Text）**：指定 VIP 花字的 fancy\_style\_id。  
  * **排版控制**：border\_width（描邊粗細）、border\_color（邊框顏色）、shadow\_color（投影顏色）30。  
  * **字幕動畫（Text Animation）**：在 materials.material\_animations\[\] 中注入特定的動畫（如 blur-text-in 入場或 typewriter 打印動畫），設定其持續時長，並在 segment 中建立引用的關聯關係12。

### **11\. 字幕 VIP 貼紙關聯注入**

* **執行機制**：調用 /jianying-inject-tts-sticker 技能13。  
* **底層行為**：系統基於當前字幕的內容語意，從雲端 VIP 貼紙庫中匹配適合的貼紙素材。在 materials.stickers\[\] 中插入貼紙的 metadata（包含雲端 resource\_id 與寬高變換比例）12。隨後在時間軸上創建專門的貼紙軌道，插入一個與對應字幕 target\_timerange 起止時間完全重合的貼紙 segment12，實現「字幕出現，貼紙同步浮現」的畫面感。

### **12\. 人工干預二：加入背景音樂（暫停點）**

* **執行機制**：LangGraph 再次觸發中斷：interrupt("等待人工加入 VIP BGM")1。  
* **底層行為**：工作流暫停，等待剪輯師在剪映用戶端中打開該草稿，從其付費音樂庫中挑選、修剪並拖入一首 VIP 背景音樂3。完成後，用戶點擊恢復按鈕，工作流重載最新的草稿數據2。

### **13\. 音訊優化與音量調整**

* **執行機制**：工作流讀取步驟 12 中人工寫入的 BGM 音軌10。  
* **底層行為**：在 draft\_content.json 中定位該音訊的 segment12。調用音量優化 API，將背景音樂的 volume 參數調整為 \-18.0（分貝），以防壓過語音旁白15。同時，在 materials.audio\_fades\[\] 中為該音軌在時間軸首尾端分別注入一個 1.5 秒的 fade\_in\_duration（淡入時長）與 2.0 秒的 fade\_out\_duration（淡出時長），實現聲音的平滑過渡12。

### **14\. 豎屏封面製作與本地橫屏多尺寸導出**

* **執行機制**：調用 /jianying-make-cover 技能12。  
* **底層行為**：  
  1. **9:16 豎屏封面**：調用剪映 VIP 封面功能，擷取影片第一影格或指定畫面，添加標題文字，並將配置記錄至 cover\_info（包括封面在影片中的時間點與裁切寬度），輸出 draft\_cover.jpg5。  
  2. **橫屏封面生成（16:9 與 4:3）**：Python 腳本讀取當前生成的豎屏封面底圖，調用 Pillow 庫進行邊界外擴與智慧模糊填充（Background Blur）30。接著，利用 PIL 繪圖模組在重新計算後的中心坐標上，以 16:9 比例與 4:3 比例重新渲染與豎屏相同的主標題與輔助裝飾元素，並在本地直接保存為 cover\_16\_9.png 與 cover\_4\_3.png13。

### **15\. 封面無痕英文本地化（全程不進剪映）**

* **執行機制**：調用 /jianying-cover-localize-en 技能。  
* **底層行為**：此步驟要求直接對本地中文豎屏封面的底圖進行修改，完全繞過剪映 GUI 界面。  
  1. 系統加載中文豎屏封面圖像。  
  2. 調用 FireRed-Image-Edit 模型，檢索中文文字區域的 Bounding Box，生成精確的文字像素掩膜（Mask）20。  
  3. 利用 AI 局部修補演算法（Inpainting）將中文文字無痕擦除，重建被遮擋的背景紋理與光影效果20。  
  4. 調用 Pillow 圖像處理模組，加載英文專用字體，在擦除後的底圖上，依據原中文排版風格（字號、字距、傾斜、立體花字邊框）重新寫入翻譯後的英文標題30，最終輸出 cover\_9\_16\_en.png。

### **16\. 目標草稿「吉隆坡武吉免登\_純視頻」字幕翻譯（異常暫停機制）**

* **執行機制**：調用 /jianying-translate-subtitles 技能，明確指向名稱為 吉隆坡武吉免登\_纯视频 的剪映草稿。  
* **底層行為**：  
  1. **精確路徑檢索**：系統遍歷本地草稿目錄（如 com.lveditor.draft 根文件夾），讀取各草稿目錄下的 draft\_meta\_info.json，鎖定 draft\_name 或項目名稱為 吉隆坡武吉免登\_纯视频 的目標文件夾9。  
  2. **字幕文本翻譯**：解鎖讀取該草稿的 draft\_content.json10，遍歷所有 materials.texts\[\] 陣列，提取其中文旁白字幕12。調用翻譯代理人，將中文文本轉換為對應的英文字幕6。  
  3. **UI 介入異常暫停機制**：在執行過程中，如果系統檢測到任何由於字數拉長導致的排版重疊，或者遇到了需要手動介入微調（例如調整文本框框寬度、調整中英混排邊界、字體加載異常等）的情況，LangGraph 將調用 interrupt("檢測到需要進入剪映界面操作，現已停下來通知用戶。請手動校對。")1。系統發出通知並中斷掛起，等待用戶在剪映介面中操作完畢、回傳確認信號後才繼續執行。

### **17\. 注入英文 AI 配音（限制：不加貼紙）**

* **執行機制**：調用 /jianying-inject-english-tts 技能。  
* **底層行為**：  
  1. **英文語音合成**：將步驟 16 翻譯出的英文字幕發送至 FireRedTTS 26。合成出高品質的英文 .wav 旁白配音，並精確測量該音訊文件的微秒級時長 ![][image10]38。  
  2. **配音寫入與音畫對齊**：在 draft\_content.json 的 materials.audios\[\] 中註冊新配音，並在 tracks 中新建一條語音軌道，插入對應 segment12。為保證音畫完美對齊，若影片鏡頭原時長 ![][image11]，則調用常規變速 API 微調該場景的分鏡播放速度，使其精確匹配配音時長12。  
  3. **貼紙抑制邏輯（不加貼紙）**：在執行此技能時，腳本內部的 inject\_stickers 標記被硬編碼或動態傳參設置為 False。工作流將自動繞過貼紙檢索與素材綁定模組，**完全禁止**在 materials.stickers\[\] 中插入任何新對象，且不在時間軸上為英文配音創建任何貼紙 segments12。

## **工作流狀態機與人機協同設計**

下表詳細羅列了整個 17 步工作流在 LangGraph 狀態機中的運行模式，清晰地區分了全自動執行與人工介入的中斷條件1：

| 步驟編號 | 步驟功能名稱 | 調用技能 (Skill Identifier) | 狀態機運行模式 (Execution Mode) | 中斷/暫停觸發條件 (Interrupt Trigger Rules) |
| :---- | :---- | :---- | :---- | :---- |
| **Step 1** | 清理舊的快取文件29 | /kuaishou-clean-cache \[cite: 29\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 2** | 啟動 OpenStoryline 服務22 | /openstoryline-launcher \[cite: 22\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 3** | VS Code 內嵌預覽啟動17 | (VS Code 系統插件加載)18 | **全自動 (Autonomous)** | 無中斷 |
| **Step 4** | 智慧分鏡輸出場景 | (OpenStoryline 底層 VLM 規劃)22 | **全自動 (Autonomous)** | 無中斷 |
| **Step 5** | 導出草稿至剪映9 | /openstoryline-to-jianying \[cite: 9\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 6** | 人工調整分鏡順序 | (人工在剪映客戶端內操作) | **人工介入 (HITL)** \[cite: 3, 7\] | **靜態中斷**：寫入草稿完成後，調用 interrupt() 強行掛起工作流，等待 resume 信號1。 |
| **Step 7** | 分鏡變速（![][image1] 秒限制） | /jianying-speed-fit-35s \[cite: 30\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 8** | 添加草稿字幕 | /jianying-add-subtitles \[cite: 13\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 9** | 注入 VIP 轉場與特效13 | /jianying-inject-fx \[cite: 13\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 10** | 字幕花字與動畫調整 | /jianying-inject-text-fx | **全自動 (Autonomous)** | 無中斷 |
| **Step 11** | 字幕 VIP 貼紙關聯13 | /jianying-inject-tts-sticker \[cite: 13\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 12** | 人工加入 VIP BGM | (人工在剪映客戶端內操作) | **人工介入 (HITL)** \[cite: 3, 7\] | **靜態中斷**：特效寫入完成後調用 interrupt()，等待人工添加背景音樂完畢並確認1。 |
| **Step 13** | AI 音量與音效優化 | (後端音量調諧腳本)15 | **全自動 (Autonomous)** | 無中斷 |
| **Step 14** | 多維度封面渲染12 | /jianying-make-cover \[cite: 12\] | **全自動 (Autonomous)** | 無中斷 |
| **Step 15** | 封面無痕英文本地化20 | /jianying-cover-localize-en | **全自動 (Autonomous)** | 無中斷 |
| **Step 16** | 吉隆坡草稿字幕翻譯6 | /jianying-translate-subtitles | **混合模式 (Hybrid)** | **條件中斷**：若翻譯代理人檢測到排版重疊、字體缺失或需要人工介入 UI 微調，拋出異常並暫停3。 |
| **Step 17** | 注入英文 AI 配音 (無貼紙) | /jianying-inject-english-tts | **全自動 (Autonomous)** | 無中斷 |

## **技術選型對比與效能指標分析**

為了讓剪輯團隊與運維部門充分理解為何本方案拋棄了傳統的 UI 自動化（如 RPA、PyAutoGUI）或單一腳本直接寫入（CapCut-cli 直連）30，下表從系統健壯性、執行效率和安全策略等方面進行了多維度對比：

| 評估與對比指標 | LangGraph \+ pyJianYingDraft (本系統方案) | 傳統 UI 自動化 (影刀 RPA / PyAutoGUI) | 單純命令列直連 (CapCut-CLI / Python 直寫) |
| :---- | :---- | :---- | :---- |
| **執行機制本質** | 狀態機驅動的底層 JSON 物件操縱3 | 外部模擬屏幕坐標點擊與鍵盤映射11 | 單向命令列調用，無複雜狀態維持能力11 |
| **平均剪輯響應時間** | **毫秒級 (10ms \- 200ms)** \[cite: 11\] | **極慢 (數秒至數分鐘，受 UI 響應限制)** | **毫秒級 (50ms \- 500ms)** \[cite: 11\] |
| **人機協同支援能力** | **極佳** (支持原生 interrupt 與 Command 掛起恢復)1 | **極差** (人機爭搶鼠標會導致整個 RPA 流程崩潰) | **無** (必須一氣呵成執行，中途無法安全暫停) |
| **抗軟體 UI 變動干擾** | **極高** (直接操作數據層，無視軟體介面改版)11 | **極低** (剪映更新按鈕位置或彈窗即會導致腳本失效)11 | **極高** (同樣不依賴 UI)28 |
| **VIP 效果與花字支持** | **支持** (通過模板模式與資源 ID 指向完美繞過)6 | **支持** (但需要模擬極為複雜的滑動下載操作)42 | **部分支持** (能手動寫入屬性，但複雜模板替換難度高)6 |
| **異常恢復與容錯機制** | **極強** (基於 Checkpoint 機制，可隨時重試或回滾)4 | **無** (出錯後必須重啟整個電腦或 RPA 線程) | **受限** (依賴命令列級別的備份還原)30 |

## **系統部署與運作維護建議**

為了保障該 AI 影片剪輯自動化系統在實際生產環境中的高可用與穩定運行，運維團隊應嚴格執行以下部署與監控規範：

### **1\. 環境安全降級與網絡阻斷**

由於剪映 6.0.0 以上版本本地草稿檔案預設實施 AES 強制加密，這將阻斷 pyJianYingDraft 與 capcut-cli 的寫入通道5。

* **部署要求**：  
  1. 在剪輯工作站上彻底卸載剪映 6.x/10.x 等新版本5。  
  2. 降級並安裝 **JianYing Pro v5.9.0 官方版本**（在此版本下，draft\_content.json 保持明文 UTF-8 JSON 格式，API 可完美雙向讀寫）5。  
  3. 在系統主機防火牆或 hosts 文件中，將剪映的自動升級域名（例如 lf3-faceec.bytetos.com）指向 127.0.0.1，徹底屏蔽剪映的靜默升級路徑5。

### **2\. 持久化檢查點與事務安全**

* **部署要求**：  
  1. 在 LangGraph 中，必須棄用測試級別的 InMemorySaver1，在生產環境中配置強一致性的 AsyncSqliteSaver 或 PostgresSaver 作為持久化 Checkpointer4。  
  2. 對 draft\_content.json 的每一次自動化寫入，必須封裝在臨時重命名原子操作中（先寫入臨時文件 draft\_content.json.tmp，驗證其 JSON 格式合法性後，再執行操作系統級的覆蓋重命名）31。這能有效防範在寫入大體量草稿時因突發斷電、內存溢出（OOM）而導致的草稿損壞31。

### **3\. 字幕與音訊時長邊界校對**

* **部署要求**： 在步驟 17 注入 AI 英文配音時，配音的時長與影片分鏡時長往往存在細微偏差38。系統必須配置動態速度補償閾值。若時長偏差在 ![][image12] 以內，建議使用常規變速微調影片片段時長12；若時長偏差過大（例如英文單詞量過多導致配音嚴重拉長），系統應自動向 Web 端發送預警信號，由剪輯師在 VS Code 的 Edge 預覽介面中進行可視化的文本截斷與微調16，保障最終導出影片的視覺與聽覺體驗達到影視級水準。

#### **引用的著作**

> 1. interrupt | langgraph \- LangChain Reference, [https://reference.langchain.com/python/langgraph/types/interrupt](https://reference.langchain.com/python/langgraph/types/interrupt)  
> 2. Interrupts \- Docs by LangChain, [https://docs.langchain.com/oss/python/langgraph/interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)  
> 3. LangGraph Uncovered:AI Agent and Human-in-the-Loop: Enhancing Decision-Making with Intelligent Automation Part \-III \- DEV Community, [https://dev.to/sreeni5018/langgraph-uncoveredai-agent-and-human-in-the-loop-enhancing-decision-making-with-intelligent-3dbc](https://dev.to/sreeni5018/langgraph-uncoveredai-agent-and-human-in-the-loop-enhancing-decision-making-with-intelligent-3dbc)  
> 4. How I implemented human-in-the-loop with LangGraph's interrupt pattern — full breakdown : r/LangChain \- Reddit, [https://www.reddit.com/r/LangChain/comments/1s6qidj/how\_i\_implemented\_humanintheloop\_with\_langgraphs/](https://www.reddit.com/r/LangChain/comments/1s6qidj/how_i_implemented_humanintheloop_with_langgraphs/)  
> 5. JianYing 6.0+ draft\_content.json encryption: detection, workarounds, and what doesn't work, [https://gist.github.com/renezander030/521e6c6e8590a2a6e917009d9313bc55](https://gist.github.com/renezander030/521e6c6e8590a2a6e917009d9313bc55)  
> 6. GitHub \- GuanYixuan/pyJianYingDraft: 轻量、灵活、易上手的Python剪映草稿生成及导出工具，构建全自动化视频剪辑/混剪流水线。本项目的CapCut版本正于https://github.com/GuanYixuan/pyCapCut 内开发· GitHub, [https://github.com/GuanYixuan/pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)  
> 7. Human-in-the-loop \- Docs by LangChain, [https://docs.langchain.com/oss/python/langchain/human-in-the-loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)  
> 8. Has anyone successfully used Temporal as the runtime for long-running AI agents? \- Reddit, [https://www.reddit.com/r/LangChain/comments/1uxresv/has\_anyone\_successfully\_used\_temporal\_as\_the/](https://www.reddit.com/r/LangChain/comments/1uxresv/has_anyone_successfully_used_temporal_as_the/)  
> 9. GitHub \- notinmood/JianyingDraft.PY: 根据资源文件自动生成剪映草稿的实现, [https://github.com/notinmood/JianyingDraft.PY](https://github.com/notinmood/JianyingDraft.PY)  
> 10. capgenie \- PyPI Package Security Analysis \- Socket.dev, [https://socket.dev/pypi/package/capgenie](https://socket.dev/pypi/package/capgenie)  
> 11. Automate Video Creation with AI: A Deep Dive into JianYing MCP Server \- Skywork, [https://skywork.ai/skypage/en/automate-video-creation-ai/1980461183976906752](https://skywork.ai/skypage/en/automate-video-creation-ai/1980461183976906752)  
> 12. CapCut / JianYing draft\_content.json schema cheat sheet: every top-level key, what it does, version differences \- Gist, [https://gist.github.com/renezander030/80823f1d47081c312d2c1f9edd20dc22](https://gist.github.com/renezander030/80823f1d47081c312d2c1f9edd20dc22)  
> 13. 【剪映小助手】保存剪映草稿-腾讯云开发者社区, [https://cloud.tencent.com/developer/article/2598230](https://cloud.tencent.com/developer/article/2598230)  
> 14. pyJianYingPro \- PyPI, [https://pypi.org/project/pyJianYingPro/](https://pypi.org/project/pyJianYingPro/)  
> 15. pycapcut \- PyPI Package Security Analysis \- Socket.dev, [https://socket.dev/pypi/package/pycapcut](https://socket.dev/pypi/package/pycapcut)  
> 16. \[Deprecated\] Browser Preview \- Visual Studio Marketplace, [https://marketplace.visualstudio.com/items?itemName=auchenberg.vscode-browser-preview](https://marketplace.visualstudio.com/items?itemName=auchenberg.vscode-browser-preview)  
> 17. Use the Visual Studio Code extension | Microsoft Learn, [https://learn.microsoft.com/en-us/power-pages/configure/vs-code-extension](https://learn.microsoft.com/en-us/power-pages/configure/vs-code-extension)  
> 18. Microsoft Edge Tools for VS Code \- Visual Studio Marketplace, [https://marketplace.visualstudio.com/items?itemName=ms-edgedevtools.vscode-edge-devtools](https://marketplace.visualstudio.com/items?itemName=ms-edgedevtools.vscode-edge-devtools)  
> 19. Debug Microsoft Edge in Visual Studio Code, [https://learn.microsoft.com/en-us/microsoft-edge/visual-studio-code/debugger-for-edge](https://learn.microsoft.com/en-us/microsoft-edge/visual-studio-code/debugger-for-edge)  
> 20. FireRedTeam \- GitHub, [https://github.com/FireRedTeam](https://github.com/FireRedTeam)  
> 21. FireRed, [https://fireredteam.github.io/](https://fireredteam.github.io/)  
> 22. FireRed-OpenStoryline is an AI video editing agent that transforms manual editing into intention-driven directing through natural language interaction, LLM-powered planning, and precise tool orchestration. It facilitates transparent, human-in-the-loop creation with reusable Style Skills for consistent, professional storytelling. · GitHub, [https://github.com/FireRedTeam/FireRed-OpenStoryline](https://github.com/FireRedTeam/FireRed-OpenStoryline)  
> 23. FireRed-OpenStoryline, [https://fireredteam.github.io/demos/firered\_openstoryline/](https://fireredteam.github.io/demos/firered_openstoryline/)  
> 24. Crayotter: Traceable Multi-Agent Workflows for Long-Form Video Editing \- arXiv, [https://arxiv.org/html/2606.07636v1](https://arxiv.org/html/2606.07636v1)  
> 25. FireRedTeam repositories \- GitHub, [https://github.com/orgs/FireRedTeam/repositories](https://github.com/orgs/FireRedTeam/repositories)  
> 26. 剪映導出SRT 字幕工具網頁版, [https://xstar.me/JianyingSRT/](https://xstar.me/JianyingSRT/)  
> 27. 如果是新版剪映加密的json可以解析吗 \- CSDN文库, [https://wenku.csdn.net/answer/6j94236s7x](https://wenku.csdn.net/answer/6j94236s7x)  
> 28. Automate YouTube Shorts with CapCut: The CLI \+ Claude Pipeline \- René Zander, [https://renezander.com/guides/automate-youtube-shorts-capcut-cli/](https://renezander.com/guides/automate-youtube-shorts-capcut-cli/)  
> 29. 快手数据分析工具怎么清除 \- 帆软, [https://www.fanruan.com/blog/article/122785/](https://www.fanruan.com/blog/article/122785/)  
> 30. capcut-cli | Yarn, [https://classic.yarnpkg.com/en/package/capcut-cli](https://classic.yarnpkg.com/en/package/capcut-cli)  
> 31. CapCut MCP | MCP Servers \- LobeHub, [https://lobehub.com/es/mcp/burnshall-ui-capcut-mcp](https://lobehub.com/es/mcp/burnshall-ui-capcut-mcp)  
> 32. FireRed-OpenStoryline/docs/source/en/guide.md at main \- GitHub, [https://github.com/FireRedTeam/FireRed-OpenStoryline/blob/main/docs/source/en/guide.md](https://github.com/FireRedTeam/FireRed-OpenStoryline/blob/main/docs/source/en/guide.md)  
> 33. Darrenpig/Feishu-mcp-AutoClip \- GitHub, [https://github.com/Darrenpig/Feishu-mcp-AutoClip](https://github.com/Darrenpig/Feishu-mcp-AutoClip)  
> 34. Live Preview \- VS Code Extension \- Visual Studio Marketplace, [https://marketplace.visualstudio.com/items?itemName=ms-vscode.live-server](https://marketplace.visualstudio.com/items?itemName=ms-vscode.live-server)  
> 35. Edge DevTools Extension Fails to Launch Visible Browser and DevTools in VS Code on Windows 11 · Issue \#2147 \- GitHub, [https://github.com/microsoft/vscode-edge-devtools/issues/2147](https://github.com/microsoft/vscode-edge-devtools/issues/2147)  
> 36. FireRed-OpenStoryline vs Memories.ai: Two Approaches to AI Video Editing, [https://memories.ai/blogs/firered-openstoryline-vs-memories-ai-video-editing](https://memories.ai/blogs/firered-openstoryline-vs-memories-ai-video-editing)  
> 37. 使用Python创建剪映草稿，并导入图片和音频放在轨道上，图片对齐音频 \- CSDN博客, [https://blog.csdn.net/a820206256/article/details/134428639](https://blog.csdn.net/a820206256/article/details/134428639)  
> 38. Pilipili-AutoVideo \- AI Agents on GitHub | SkillsLLM, [https://skillsllm.com/skill/pilipili-autovideo](https://skillsllm.com/skill/pilipili-autovideo)  
> 39. 剪映AI影刀RPA，智能创作效率翻倍-知识星球, [https://wx.zsxq.com/group/51285485258154/topic/1522144822825452](https://wx.zsxq.com/group/51285485258154/topic/1522144822825452)  
> 40. README.md \- dreamsncode/CapCut-AI-Agent \- GitHub, [https://github.com/dreamsncode/CapCut-AI-Agent/blob/main/README.md](https://github.com/dreamsncode/CapCut-AI-Agent/blob/main/README.md)  
> 41. 影刀RPA是如何实现剪映自动化的？从0开始教你搭建自动剪辑视频机器人, [https://www.bilibili.com/video/BV1jb4RzKEtf/](https://www.bilibili.com/video/BV1jb4RzKEtf/)  
> 42. CapCut Auto-Downloader Script | PDF \- Scribd, [https://www.scribd.com/document/981098362/Ur-Mum-Scribd](https://www.scribd.com/document/981098362/Ur-Mum-Scribd)

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACUAAAAWCAYAAABHcFUAAAAAo0lEQVR4XmNgGAWjYBQMDSAPxOFAzIUuQU/ABMTGQLwEiLWBmBFVmv7ADohXA/EUIFZBk6M7YAdiXyCuBWIpNDm6A04gDgPi/UBciiY3IADkoB0MEEeB2IMG8ANxLgMkpBLR5AYccANxDAPEgSCHDioASuygKK1mGAQJHhsAlVegsgpUPCiiyQ0oABWcoAIUlDN50ORGwShAB6D0IkwkHpnpCQCq5g2xF9Xp1gAAAABJRU5ErkJggg==>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAD0AAAAaCAYAAAAEy1RnAAACkElEQVR4Xu2X28tOQRTGH2c5RE5JyoccEyEKESKllJAQhSgK4cJZcggplHJ24ZobUlzJnTslVy7d+yN4fq2ZbNPW937iYn/ffupXe8+8M3vWmrXWzCu1atWqVavmaoA52wM2xbBma7OZXLS9N9/NykrbQHPLnKy0NVKDFYaUwmAMn1S0X1I4qdHqMrfLRuuHuatwShZpcNUsqbQ1UuTngaJtmMLovTXtNxSO6nXaYN6a8WVHb1U/c9HcVIRzn9Bo89LsLDt6s2abT2ZB2dEAHTPny8buRDgT1hQxwrxpog7NKxu7E4WLgRj9J+GMxQqPPjarzQjzQOHp7KwzCifSd8rsVxx33AlOKC46D801c8Rc16/COdwcV3yDMaMU85B228w+c1kxL/NMMXfMV/PcHDT91YE4j9eab+aL6gdhEB99ZsaYN4qF7TJ7FIYPVRh7KI1ZZRaaV2Zq+g3wvNR8NmsUNWSGwrj7ZjeDrR2KlOM3zPnRzE19L8zY9IyuqMMIzYWL3a2D4yuLhX4w6xUeXmQmKHb7kcJwxI4tS8/08Z77ppuJZqbCUMZx/rMOFsydgZvgLLPVnE79yxWXpTwPYt2MQyP1n26KLLLuaoremWnpmSJY/Q3Pdbc46kf1Hk+UEAVPFDs7Tr/vHN/I8+SjNfd3mTnp+Z8KTz5VeB5RNAhtlM/1IeaewvCNqY+wrF5nszCiq/KeDcmpQYptN/PTe/VaTCQRQUQGTsV5fH+d6h381yLfyBuKFLnMzmMkeq24tp5Lv7mgWBSLZLF1yqFdFaGPY3EmRYwUwhnMUw1t0uOoWZHe+Td42GxRh3ndU2F8WejIcfIrt1d3lrCt06CyIYlFU7FLld8sHVa+t2rV1/QTQHlhOsTMRUcAAAAASUVORK5CYII=>

[image3]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABYAAAAaCAYAAACzdqxAAAABRElEQVR4Xu2TLUsFQRSGjx+IiKIIFtM1CDY1GEWbwS4YhBsFm1hMggZBg8UvELOgYBSLyWy1WOz+CH2fe3ZwZnb1usmyDzwsO2d35sw7u2YNDd2Yl3vyXj7KpbRcYtP8ObyRO2m5DC8cy3M5mNUCE/JCfsqVrFbJgDyUq/JOjqXlDj3miz/IZ9lKqj9AJ5dyWj7JybTcYc584ld5JYfScjUL8kCOyBc5k5ZtWO6aN0AM22m5mhDDbHHPi+vfZZsyP6B+84lrx8AV8o6YlMmBxWvH0FfcM/G++WHBWnGFDftjDNA2fyFAxuyArtgF+QKRncjl4v5X8hiAz40cyXkrGmdn71YjhiPzbgL8IB/y1tIF2RUxdYVTvpancjwa5/d+k4vR2Kg8M5+4Nxov0TLfLg8Gw6HQGd8sCwPdx8/ln2NDw3/wBdoaOQrjpbgiAAAAAElFTkSuQmCC>

[image4]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJoAAAAaCAYAAABPT0XPAAAFd0lEQVR4Xu2a2YskRRCHf97neosHHqOieOKFx6Ir64WsIKiI4D14oYiiIh6IrAceeIL3jSsqggqioAj6oiCC6IMPIvjSb/vgH6HxGRVUTk5NdfV09fQynR8E052VVV2V+cuIyKiRCoVCoVAoFAoTYm+zh8zOMdsqO1Yo9MIxZu/JxcbfUxYeLhTGZ1+zT83OlAvsT7O1C3p0ZBu5S+xql/hpM8328tW9a36gYq+8wdhH0w05++UNGTzLwWbbZu2Xmn0nF9xYcKGDsrYfzP4xW5e0cQPPmt2btM0SiOQes1fNdqnaTjL7w+zn6GTsbPam2b+JbTbbIemzEuxp9rfZ79Vf7iMHcb1u9qG8P5wr91pXq34WvpObIcTPzI6t+naGlYl4chAZYjswa98oF+Ysgpf6Wj5hT1RteLVvq7YgJieNALsnx1eKreVCSoWfc6F8ri9K2tDES3IvFufy3Dw/398xe0Qjeuc5sxfyRvlN8WP8aECIZYBPS9pmCcRCrtJVaMsBcXB+G8xJOi/DaBMac4y3OjFrJ2rRnzl/Rn4+14lrxffOsNpuztq4AD8y39D+tFycswpiO1l1GDza7DezQXTQeEIjD9pkdkF+oIIU5wOzM7L2NtqExiLh/nmOlOtU90cfhNY1qq+F+BDhWOBGv1EPyd+UOUueb3S1E9Q9HBwnDyG/ml2jhflXTMaDZoebHSkvCRyV9BkGY0+OlN7P5/J7HJU2oSGyNqFxLvfAvTwvj353avGGYWS4KPG3F8VOmUkL7Qb5giT8HJIc4xqPVn8DkugvNNrifcvsenk45d6WIzIYV2gBOdoB6j5Grewh31VclR8oNEIo+14+WW1EOnJFfqCF3eQ7QtIVvNly6UtovRI5R54cplC0a6oTTZq7zB7OG6cMq/tx+aREeaAJ+tGHvqN4BHK1gdl9WfsobHFCix0GP7DUYMTWdxoQpo7PG1v4Uf4sXe1Fsx3/P7MZkmL6UZNKX8HMV+3sxtNJTUsaXJe2rguF8ScB37/6Tk5EqWQ5tbg2oeExB1pcSeA+m/r3AvkDk9n0AwwUxcpN8oHGnVPpBlz8k2bPVX1uktdv7peXA26U1+o4FnkfsZ5rcN4rZhuqdgb4VNU7GzYmh8pF8Jd8x3WLPG9ZaWLLP1A9MalHi5ojk/RR9TkgJRnIX+MMg2sSYvN8jiR8o9lOWfsw2oTG79DeVEdLi9C9wcXPkw8Gle6lJpIBpngXIKjX5INDOCXhnZdXkSkDMDlfylcOFh4DAZ5efWZntr76TFLOMVYydnvVDqOGnb7hLQC7zCtV38dhcs+5OTrJq+bvJt/pe63cIw3brdGXHd4b+QH5uXjVp7T0q68mGMdP5ILKfx8xM2dvq74mGx3CKY6hNyL5j/CRW6p0uE0LQ+dlqoXHiqYugxjXyx8CcRHvj1AdBhhMVljEf/LBOfn5VKPvkBdCH1MtxjWa/hsJ7pud7E/ysMZ9/lJZXveiHx4bcVEGGahb2GNzwTlLCQkHgBc6Pz/QQGxAmuzspB9ll6/k0YpNIJ6MysOonrM3wqXOV9/DJeNpILxduPxUeCl4vvnkOwJCSGvl3hRh4tnS6vec/F9VtgTwCnhrFiF/cy8RsEgulk/qNDZPoxDPxLgPe/k+cRAMwkFAiIEXywiNAcdbESLxeFG1Du+Xvy7Bi8aKojYUuQA7H7bxkeuwum81204egsnv8By5cAurDDzV+/I8IVw7IQKx4e7vliftbALC+yHIJj6Wh0YSfl7eQ+QnVKARL0KN/5JYJ8/XLq/6FVY5uFh2mSmIKiYfAUYoIfFfalMREDbJYVK4XlOomUhNp7A6ISF+IPn8sha/1C8UegGPx66yeKhCodAf/wFUuw1obkSP4gAAAABJRU5ErkJggg==>

[image5]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAI0AAAAaCAYAAACKPd9eAAAE5UlEQVR4Xu2a26vVRRTHl9duonYTiZSTmmSEUtINLUyMIAgqRVIUztEegryTly5amNpFUois1BAFHyoJkqCewodA6EEQH3rpxXf/CF0f1izO7HV+O87vd/Y5nb2dL3w5e8/85jcz31mz1prZR6SgoKCgoKCgoKCgoKBg+JikfL8GX7NmBRVAy7UyVLN2REvadB1eVz4cyv5U3lC+kJVNVn6p3JWVFbQCLX+VVj0fEtMz1xJ0rZZTxQYfgcEwUSac42MxYXoFc5UzYmFDuJYrQjnGgp49o2Wf8qtYqLipPC4mhAM3+qny6ays2/Gc8iflAuWEUFcXfWJaTgvleBP0zLUEXaslMfXtUHa32CT7K8o/ExOnlzBReUZ5XrksfW+CdlqeFtMzoqe0fEX5u/LBWHGb4H7lduW7MvLQhZYYDHr2LHDR+5WfS5dm9R0Ep6C/xPSI+chw4FpiNOjZs5ipvKBcFytuU3BiXCV2Iqqb97iWGE1P6/mY8opySazoAjwltkj3xYoRghyHE9A3MjTJ/S+4lv9Iez1Ha8zDQUf6JRzhRtkZdXbUeAAnE057MRFtCnKZS2K5DTlOE7iWB6Vaz06PuQ7ouyP9kviSsFVl+g4mv1T5ofKk2J0Eu+9b5bZUD/aJGSF1e5SbxI6Y3GPsVL6p/E55SLlFeUIGE+97lF+L9UEbFpB3sDPWKAeUP6QywsedynPKf5W/iJ1KmoLchX4xmLtCXV24llV3Me3GPF0sDzosptPmVI6uuV5HpPWgQrujYrphpN5uVvr+hdj70JK/9O39PpCerQ0sb6XyuvKaVB87GTiLxoLh1n4TE3i9cqOY4SAGxvJOavOi8kmxnOCR9AwkyXxGeVX5kvJv5aNiRoYBufG9Jebmce+887LyceUTyp9l0Atw38F4mp74yFcIP4yTHAZjHAnQ87qYls9KtZ5xzD73DWLzZ1H7Ux3GnOtFjoRewNsB1sXbzVb+KPY8wNhyvZpq1ZKsVZEjo4NFZwe+LCYq8RhLXqH8XsxwAIN5Pn2mju9eN19sMgvFJk477jIwBITCZXITzTOrlXtTPePElft7GBfjphxgUPEycjhgMfGc3NMsTt+boo6WII75DWk1oj9k8PIv6kVfvrHYyHg1gHF5O7w0HmVOIt7HT8T0XVerRmDQVT8tAAY6L33GK+TP8Nkn7/D8Kf/tBS+FFzqlXC7mNl2YXAyAC4dej/j96XMd7FB+EgvHCHHMGESe/0TPGfUCrhntAPqgE3rRnnI8DV7K4bnUmIDYzO0mlg4IEYQmwIQwhDvE4iqG82qqq7Jq6plcX1aGWBiChzZ2PWEMDxB3JTsaQfFMblD85YdC90bjHXHMGI17Izwtcya0eXiJegE3ANrzHsKr65e/D6AVz7hheb9upKMCYieWS5JLLoPnwUjARbFd80F65iOxRW1n1Rigu9ociIVhEppwr4RAJpWHJnBeuVXs2h8QXhBlt9Q7Fv+fiGNmLmjCRuHfJo6JJfv3puer9AKErrNiepHzuTdelMrRGu1ILwDrQt9jqhUdxdhPjkOc9fLcs+BCI3h+SixMwEjiZHhH3ifvzwXkfZwgRnXXdBhVY2ZenHBcgzwhb6eXA2OJIQ3NMLpobLy3m7Qq6BBY9AHlATFje0/M23iyW1BQCfckTS8gCwpGH7cAspnb7J8fKkYAAAAASUVORK5CYII=>

[image6]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADcAAAAaCAYAAAAT6cSuAAACqElEQVR4Xu2X26tNURTGP5eEyC3CA5sHIRIicolyKR4QuUWdB4WU4kFJiFAkUsglhSeJcimXoryQEk/evCiP/gi+n7FWZ+559nFyOhvrtL/6tfeaY+651xhzjDHXklpqqaWW/oIGmFFmSDI22PRJriupMeazeWnemidmtjlp+iXzKqXR5qE5kBusXWZWPlgl4cBHMzU3WIsVzldS1NNNRSo2cgLnKpuSNI/n5rtZm9lQLR+ompaYL+ZHwnuzQr2gS6JJ5pTqHcTh+emkRH3NCEVaV0bjzGZFg8HBI/XmX8KhM+aoeZTZ/itxaDfSJoVzx3ODtcDcMnvM/cz2J+K/z5ltuaEnxA7QDRtptfmqcCTXVdOWD3ZD88xTMz439IRqih3KxaPXDXNZHWtqi6IWX5grZloxTuNZZK4pUvm0mVDY0HZzUZHK/I55r80nxVFUitq/UHDQ9DdjFQ8ZW81Zc0lxH7/VMvPOzEzGBpkTilpi0UbCsfzAX27uKX5DwNJzk2DtVTShheabIiVxtq2YgwjOAzNREdTrZqXZqJhLxmBjtxv1gjoRmXWK6N01h80bRT3RWDoTi49Mruma1N7O4ppa5GbKet6g9tQbbuYU15yvpCYq1+BpCeEcu3tMsQmPFUcWGTJdsU6nIooziu/sFi2fOsOprs629MYRf/pB7Sma7shARcTzp5yy3srdzddg/JnaA0YgasX3pomIEYRU3NgdM1QRmFdmstmt2GGcLUVQOWoYY4eZz3XuLHW5o7ATGLKs6aqpY71RU7wWUVe8WdxW3Myawj5F0e6pHc7HuYoGwzHA/KWKxrHPnDfrzSpFIBABJYBNFw2BHcpFhIcp0pUbTV92ETWVj3Gdn7PUGnNzdVUq3RYL71ccD4cyW68Qu0FE+WzpX+knJ4FkLu5O+1AAAAAASUVORK5CYII=>

[image7]: <data:image/png;base64,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>

[image8]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAcAAAAcCAYAAACtQ6WLAAAAlUlEQVR4XmNgGOqAF4gZ0QVBgAWIXwOxJboECPAD8V4gVkAThwM2dAG8QAaIFwHxISCWQJbgAuIWINYCYk0gjkSWNAXiAgaI8wOA2BVZEuRskA52IJ4ExFLIkjCgBMTHGHAEQAoQP0AXBAEeIJ4PxGuBWBqI7ZElbYD4P5RuZoCEFByIMkB0TWeAOA4DgIJNGF1wmAAAYOYOznBrMiQAAAAASUVORK5CYII=>

[image9]: <data:image/png;base64,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>

[image10]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACkAAAAaCAYAAAAqjnX1AAAB/ElEQVR4Xu2Wvy+dYRTHv6V+tBTxI2maDgykdCDEIJEOTcrUxCAWNUpILEQ0ImJp0m4SgwgDsYhg6VCV6MLCYDFYujQx+iPa79d58dzjvi+S3ivifpLP4Dn3zT33nPOcF5AjR47HQS2doMv0Z+Q8nQz8SEuiz98rHfQHrfEBUke3aYsPZJsh+o3m+0BEL52jRT6QLYphLR7wgYA3dB82HvfCK/qLNvtAgJI8QfJnMopa/RfxrRY99AiWbNZ5ThdgScbxhE7TDVrhYnelklbBvvfW1MJmTVWKQzdeN38clvAwXaebdAo200JffEi/08+0IDoXz2Ad0zrrhq28L0E8ET2gKqpKcXygZ7BVdIFmUyPgUdJl7uwlXaV9wVkp7LImjdg5L2APK0kl61HVPsES1FIPUctm3Nlb2DMhelFswdrs6YxMRJdAbVaL6l1MDNJT2DyqXSFq7SwtDP7WOHiOYW1OhwpzY5L9sCqu0fLoTJWohrXygHbRvCgWouSWcHUBNGvvr8KX/KHt/jBiDAnbQlVT9ZRgOn/TFVyvnkevSrX9NR2lT1PD52j/ag971H6NgWYzo+iHNtERpF6qkIsf4tFlVDEyji7UIpKrsYfU161uvm71VyQ/99/Yxc3/GbXRHdhubYXtx7g5zwgNuL5y0qFZbaTvkMXkcjwY/gG/M0v3sBeA9wAAAABJRU5ErkJggg==>

[image11]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHoAAAAaCAYAAAB4rUi+AAAEjklEQVR4Xu2Z249dUxzHv4q6R9VdiCGtoG4hhKBIigeR9AEPbUkmVEdIXDPilhmEIEIiJdKkpsQtbpEQlweZhER48OLBi5eTePRH8Pv47eUsv9n7nHHOmTOzp+uTfNOetfbZZ+31u649UqFQKBQKhUKhUBgvE6Zp05zpm0pvmB7LdLPpiOr6/Z3bTM+ZvpDv1WemGXX36j7T2aYD0hdWGleYvjIdHyeMM0xfmy6KE/sph8qD4a84YRxk2iWfe776vKKYMr1gOjBOVNxies10SJxoOeeYDouDfTjF9J3ptzhRsd70qaljuvy/U8tL8tDb40QG6egHeapfDWBc0vCjpiPDXD/Ifn/KM2ATpHOi+qE4sZwkD70wTmRgaDy41zVt4FjT/aZ502SYWyxkP4xIBmwiGbrXNWMnLbwpbcNW0y9yg7eJNaZLTG+b3q0+D0Oetpucfp3pY/meYvAVweGmN1XfWCToIJ+SL56HaAs0QqTX3aYNGk0nfLU8bffaC4KBoGBPCZBhoKwco+Ed9J+aS+1lYU3QibNhj2g0m7XUcBTcIU/PROAooeZiQBy/aS/odbjmR9OZprdMn8v38P3suivl+/6B6YJsHPj8uukBecb9Vt4bDMyN8kXhoU1cL/dijlltYIvpJ9PjcWIEvKPekUoEzsmveVDuDJREanVdFtimhQ7DGPc4ORu7Tl4yzsrGFs1R6i4cg0dYAJGBkXkZ0EY2yzeYqIpRMwjsFXvG3tVB5BLJ54ZxojzvcSgrvFyJwYMhOcpGUolF/5tUS342bQxzsNP0hzxNxbMmTsC5+gnTk+oeUVj4K/LvJG9FNEQsEs9OTnWVaa/8rMnLhVn5uTbnGdOL8qaGCBkEfn+TvFYT7YO+xOA+GJpnroM+4Hv5s0Z41o7p0uzzrf/OOkT+s6ovNykoBzL0dvnCqRFHV2M8zHHy1ET6u0H1jcB5ci/DMF/KHYWa84npdHXP5vxL2vnQdFKle+QLn5SnI7IFm8/v5Gd5ruW7wBwRMCwYiVeXnKGj8/aCfTlNvl83ZeNrTefLnbRjOiGby8HAHdO18uZqWgvP76kX4p6RCXkvRQAtGoxCFLPoOv1u2qfeG4FH3im/16nyjfjIdFc1z2cMhXfSSNwrP8M+bbpMXn9Ib6S69NoV46fal7ybzUXcl98bBTg1TjYvX1M/cI64R7m4Dy9eyGZNpOyJI9+h+tfJ6Zo6SOeUUPqlsYJxeEh+nDqOYX/VwrMlnSLjPCDRGb311WyMB001jNeI/OGA7+AYKeOMEl7nkgpxpKUGh+LvBTgNJZH9ijQZOjV4e6r/jw0WSyoGPC1FMYbJ/yhytzzFk85T3SH6GT+4Gks1K6V6HpbanpqPBEZJv9NG0vN1wngOxqcPOTGMkbIpi2OHH31JviiMnryM2kvjRLP1sPw8y0Ix3MvV+FQ1Dhg5OQapmrpFHU73o/6TyskGs+qdGtvAjLzp7AXl8j3TNfLySE1uqvtjgeYppmEgEkm7sbPlWsZzYpOHU8QxGhfuuRqgRMRzdB3sE0a+WKvn2QuFQqHQn78B4AnDyB0I0lIAAAAASUVORK5CYII=>

[image12]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACsAAAAZCAYAAACo79dmAAACJ0lEQVR4Xu2VzUtWQRSHjyVGLfwoIwj82BRpQrSRFroIFMQ2tRVdKdUmBEEjWmhCCwUxN2rkQte5U7QIUQRFiFxIu/4AF/4R9vtxZrjHea+9MxRBcR94eO975t47Z858XJGCgoL/hirnr7gQBmK4IuVfTGrg5SDGDuthhft/G27DB/Au3IKDos96eD0GP5pYNAOwIwzmwPt+wGX4Cs7D76IdX3T3jMBNeM3F3rjYMvwEv8BVeAA79ZE0UpI9NR7DF/CSa+cMLTp5TfjMY3fteQJHJRtgEinJvhOtJBO4cbb53GSph8vkA7xuYkmkJGs7zmMIfhZNhhV/C++4tj75jYp6UpNtEl2zPVK64W7CDdgPu+AErHRtyRXl9HDxW5/B3pw4tacEE92CU/AWfA/XYLO5hzTCJTgn+g7CfpM31FPRXWn9Cvdy4pRT52GyrKivVAPcgSty9mgK4bHGSvvpr4aTcF9KB1qW2GUQ4jfUCewO2iytokuAcB3PSDboBVjn2qKISbZF9PyckGynk3HRY4y/Ib6idlM9h98k23Tsl+dwNDHJ8h4mtSu6JomvLONMIsRX1G4qDsomew++zJrLE5Msp5m7nCeA/7TWin4yj2Cbi1lmYXsQey2llWUsmphkua5YJd85E34kul7t59bD9mH3a+HH5FB0WRH2HX7h/hjcFPdFB3c1aPNweUyHQQNnYV30CHwYtP11WE27CfPgoM8bbEHBP8dP8Odau+ettLkAAAAASUVORK5CYII=>