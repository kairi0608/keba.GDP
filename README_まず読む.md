# このレース、政治経済で動いてます。（ショート動画 60秒・ラフ v3）

学生が馬の被り物と国旗ゼッケンで走る実写（A/B）をレース本編に使い、名目GDPの実データ順位と歴史テロップを重ねた縦型ショート動画の編集プロジェクトです。

v3 では素材の順番を入れ替え、**レーススタートを一直線の道（B）**に、**カーブから出てくる場面（A）を後半**にしました。

- 仕様: 60.0秒 / 1080x1920（9:16）/ 30fps / H.264 + AAC / -15 LUFS
- 完成ラフ: `output/rough_cut_v3.mp4`、確認用プレビュー: `output/preview_0-34_v3.mp4`
- 編集レポート: `output/edit_report_v3.md`（未提供素材・残課題・次の判断を含む）
- 台本とタイムコード: `60秒構成・台本.md`

> **動画ファイルは Git に入れていません。** このリポジトリは公開設定です。学生が映る素材（`source/*.mp4`）と、その派生物（`output/*.mp4`、`output/*.jpg`、`output/stems/ambience.m4a`）は `.gitignore` で除外しています。共有はチャット・ドライブ等で行ってください。

## フォルダ構成

| パス | 中身 |
|---|---|
| `source/` | 元の実写（**無改変**）。`student_running_A.mp4`（25.97秒・音声なし。直線→カーブ）、`student_running_B.mp4`（14.90秒。一直線の道）。`SHA256SUMS` で無改変を検証 |
| `timeline.json` | 編集の設計図。映像区間（作品時計 `work_*` と素材時計 `src_*` を別管理）、字幕、テロップ、日本タグの追跡点、効果音・BGM の割付 |
| `data/` | 名目GDP（世界銀行 NY.GDP.MKTP.CD）5か国 1960〜2023年と取得元メモ |
| `assets/fonts/` | Dela Gothic One / Noto Sans JP（どちらも SIL OFL） |
| `assets/` | 差し替え素材の置き場（下記） |
| `scripts/` | `render.py`（映像）、`build_audio.py`（音声）、`fetch_gdp.py`（データ）、`test_output.py`（自動テスト）、`build_all.sh`（一括） |
| `working/` | 中間ファイル（Git 管理外） |
| `output/` | 納品物 |

## 作り直し方

```bash
# source/ に student_running_A.mp4 と student_running_B.mp4 を置いてから
bash scripts/build_all.sh      # 約5分。.venv を作って依存を入れ、全工程とテストを実行
```

字幕の文言・表示秒・効果音の位置は `timeline.json` を直して再実行するだけで反映されます。

## 素材の差し替え（置くだけで自動的に使われます）

| ファイル名 | 作品時計 | 内容 |
|---|---|---|
| `assets/01_start_gate.mp4` | 0.0〜4.0秒 | 発走ゲート（今は B の 1 コマ目を静止させてゲートに入れている） |
| `assets/02_growth_rush_original.mp4` | 17.0〜20.0秒 | 高度経済成長 RUSH（3秒） |
| `assets/03_oil_shock.mp4` | 33.5〜36.0秒 | オイルショック |
| `assets/04_policy_cards.mp4` | 36.0〜39.0秒 | 政策カード |
| `assets/05_lecture.mp4` | 50.0〜56.75秒 | **未撮影**：大学の授業風景 |
| `assets/06_future_run.mp4` | 56.75〜60.0秒 | **未撮影**：被り物を外した学生が走り出す |

置いたファイルは縦型にトリミングされ、区間の頭から再生されます。字幕・年号・GDP盤面はその上に重なります。01〜04 の AI 制作素材は今回届いていなかったため、コードで描いた代替演出が入っています。

## 注意

- 実況・ナレーションは **Open JTalk による仮音声**です。本番は学生の声で録り直してください（`output/stems/voice_guide.m4a` がタイミングの目安）。
- BGM・効果音はすべて `build_audio.py` で一から合成しています。既存の競馬実況やパチンコの音源は使っていません。
- **募集要項は生成AI作品を禁止しています。** このラフは AI（Claude Code）が編集・グラフィック・音を作ったものです。応募規定に適合するとは主張しません。最終版の作り方は主催者に確認してください。
