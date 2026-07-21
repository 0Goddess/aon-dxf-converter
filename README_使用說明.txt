Project XML 轉 AON DXF 1.7.4
=============================

用途
----
將 Microsoft Project 匯出的 XML 轉成 AON 全區網圖 DXF。
輸出檔可使用 AutoCAD 2023 開啟；DXF 採 R2010/AC1024 格式。

使用方式
--------
1. 解壓縮完整程式包，不要只複製 EXE。
2. 執行 AON_XML_to_DXF.exe。
3. 選擇 Microsoft Project XML。
4. 選擇輸出資料夾。
5. 按「開始轉換」。

輸出內容
--------
- 全區 AON 網圖 DXF
- 節點顯示 ID、WBS、作業名稱、ES、D、EF、LS、TF、FF、LF
- 紅色：Project 要徑作業及要徑關係
- 關係線：單一 LWPOLYLINE 物件，附前後作業 XDATA
- 跨列關係優先採「水平－垂直－水平」兩轉折
- 無法避開作業框時才使用上／下通道外繞
- 非要徑作業列以減少箭線交錯為優先，再兼顧轉折、長度與版面緊湊
- 中文轉成向量線條，因此不依賴使用者電腦中文字型

重要說明
--------
- ES、EF、LS、LF、TF、FF 以 Project XML 內的計算結果為準。
- 本程式不修改原始 XML，也不需要安裝 Microsoft Project。
- 產生 DXF 不需要安裝 AutoCAD；AutoCAD 只用於開啟成果。
- 轉換大型排程可能需要數十秒，中文字向量化期間請勿關閉程式。
- 程式未購買商業程式碼簽章時，Windows 可能顯示 SmartScreen 提示。

從原始碼建立 Windows 免安裝版
------------------------------
1. 在 Windows 64 位元電腦安裝 Python 3.12。
2. 執行 build_windows.cmd。
3. 完成檔位於 dist\AON_XML_to_DXF_Windows64.zip。

版本：1.7.4
