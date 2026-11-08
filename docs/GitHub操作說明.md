# 用 GitHub 自動打包：操作說明（只需要做一次設定）

目標：不用自己裝 Python、不用自己打包，只要在 GitHub 網頁按幾個按鈕，就能得到「解壓縮就能用」的 zip。

## 一、第一次設定（約 10 分鐘）

1. 到 https://github.com 註冊並登入帳號。
2. 右上角「＋」→ **New repository**（新增專案）：
   - Repository name 填 `shift-scheduler`（英文即可）。
   - 選 **Public**（公開）最簡單。公開的只有程式碼，**您的人員與排班資料只存在各台電腦裡，不會上傳**。
   - 按 **Create repository**。
3. 在新專案頁面點 **uploading an existing file**（或 Add file → Upload files）。
4. 把本壓縮檔解開後，資料夾**裡面的所有內容**（`app`、`tests`、`docs`、`main.py`、`requirements.txt`、`github-workflow-build.yml`…）整批拖進網頁。
   - 拖曳資料夾時 GitHub 會保留資料夾結構。
5. 頁面下方按 **Commit changes**。
6. **建立自動打包設定**（GitHub 網頁不允許用拖曳上傳以「.」開頭的隱藏資料夾，所以這一步要在網頁上直接建立）：
   1. 專案首頁點 **Add file → Create new file**。
   2. 最上方的檔名欄位輸入：`.github/workflows/build.yml`
      （輸入每個 `/` 時，網頁會自動把前面的文字變成資料夾，最後檔名欄位應只剩 `build.yml`。）
   3. 用記事本打開專案裡的 `github-workflow-build.yml`，全選複製，貼到下方大的編輯區。
   4. 右上角按 **Commit changes…**，再按一次綠色的 **Commit changes**。
   5. 完成後，專案首頁應該看得到 `.github` 資料夾。

## 二、打包出 zip（之後每次發新版都這樣做）

1. 專案上方點 **Actions** 頁籤。第一次可能要按「I understand my workflows, go ahead and enable them」。
2. 左側點「打包 Windows 免安裝版」→ 右側 **Run workflow** → 版本號填 `1.0.0` → 綠色 **Run workflow**。
3. 等 5～10 分鐘，出現綠色勾勾代表成功（紅色叉叉代表失敗，請點進去把錯誤訊息複製給我）。
4. 回到專案首頁，右側 **Releases** 會有新的版本，點進去下載 `ShiftScheduler-v1.0.0-win64.zip`。

## 三、給各台電腦使用

把這個 zip 複製到每台電腦（隨身碟、雲端皆可，不需要網路），解壓縮後雙擊 `ShiftScheduler.exe`。詳見 zip 內的「使用說明.txt」。

## 四、日後更新軟體

1. 請我修改程式後，把有改動的檔案重新上傳到 GitHub（Add file → Upload files，同名檔案會被覆蓋）。
2. 重新做一次「二、打包出 zip」，版本號加一（例如 `1.0.1`）。
3. 各台電腦下載新 zip，解壓縮後**取代**舊程式資料夾裡的檔案即可，`data` 資料夾（人員與崗位）不會被更動。
