# liteproxy

通信制限のかかったスマートフォンで、Webページを軽く表示するためのセルフホスト型プロキシです。

手元のPCのChromeが代わりにページを描画し、画像・動画・JavaScript・Webフォントを取り除いたHTMLだけをスマートフォンへ返します。**元のページのレイアウトはできるだけ保ちます。**

## 特徴

- **レイアウトを保ったまま軽量化**：画像は同じ寸法の枠に置き換え、実際に使われているCSSだけを残します。
- **画像は必要なものだけ**：画像の枠には送信サイズを表示し、タップしたものだけを、選んだ画質（低・中・高・原本）で読み込みます。
- **JavaScript実行後の内容を反映**：PC側でJavaScriptを実行してからDOMを保存するため、JavaScriptで生成されるページも表示できます。
- **JavaScriptで動くUIも操作できる**：開閉・タブ・メニュー・「もっと見る」などをタップすると、PC側の本物のページで同じ操作を行い、変化した部分だけをスマホへ送ります。
- **スマホ側で外部への通信をしない**：出力するHTMLからは外部リソースへの参照を取り除きます。さらにContent-Security-Policyでも止めるので、変換に漏れがあっても外部には通信しません。
- **安全に外部公開できる**：Cloudflare TunnelでPCのポートを開けずに公開し、Cloudflare Accessで利用者を限定します。
- **本文だけの表示（readerモード）**：記事から本文だけを抜き出して表示します。ナビゲーションや広告枠、関連記事が落ち、転送量がさらに減ります。
- **処理状況が分かる**：PCが描画している間は、専用の読み込み中画面に処理の段階と経過時間を表示します。
- **転送量を記録**：ページごとに、PC側で取得した量とスマホへの送信量を記録します。

削減効果の例（スマートフォン相当の画面幅での実測値）：

| ページ | 元のページ | liteproxy経由（gzip） |
|---|---|---|
| ニュースポータルのトップ | 3.6MB | 48KB |
| 放送局サイトのトップ | 2.7MB | 45KB |
| 技術記事サイトのトップ | 2.0MB | 34KB |

readerモードでは、記事ページをさらに減らせます（百科事典の長い記事：165KB → 69KB、ニュースポータルのトップ：49KB → 20KB）。

## 仕組み

```
スマートフォン ──HTTPS──> Cloudflare（Access で利用者を限定・圧縮）
                              │
                         Cloudflare Tunnel（PC から外向きに接続）
                              │
                         PC: liteproxy（FastAPI）
                              └─ Playwright + Chrome でスマホと同じ画面条件で描画
                                 └─ 描画後の DOM を複製して変換し、軽量な HTML を返す
                                 └─ ページは閉じずに保持し、スマホでのタップを再現して差分を返す
```

JavaScriptで動くUIの操作は、次の流れで中継します。

1. 描画時に、JavaScriptで反応しそうな要素（クリック処理の登録、`onclick`、ボタンや開閉のrole・aria属性、`cursor: pointer` など）に印を付けます。
2. スマホで印のある要素がタップされると、その要素の位置（ルートから何番目の子要素かの並び）をPCへ送ります。
3. PC側では、保持しているページの同じ要素をタップし、DOMの変化が落ち着くまで待ちます。
4. 変化した要素の属性や部分HTMLと、新たに必要になったCSSだけを返し、スマホ側で反映します。
   - PC側はスマホが持つDOMの写しを、スマホと同じHTMLの解析手順で保持しているため、両者の要素の位置は一致します。
   - タップで別のページへ移動した場合（SPAのURL変更を含む）は、移動先をそのまま新しいページとして表示します。

変換では、次の処理を行います。

| 対象 | 処理 |
|---|---|
| 画像 | `<img>` 要素は残し、元画像と同じ固有サイズを持つ極小のSVGに差し替えます。サイトのCSS（例：`.card img { width: 100% }`）がそのまま効くため、レイアウトが崩れにくくなります。枠には代替テキストと、既定の画質で読み込んだときの送信サイズを表示します。 |
| CSS | 描画後のDOMで使われているルールだけを残し、画面に合わないメディアクエリ、`@font-face`、外部画像を指す`url()`を取り除きます。 |
| JavaScript | すべて取り除きます。 |
| 動画・iframe | 同じ寸法の枠に置き換えます。 |
| リンク・フォーム | liteproxy経由になるよう書き換えます。POSTでの送信は、PC側で保持しているページのフォームに値を入れて送信します。 |
| Cookie同意バナー | JavaScriptなしでは閉じられないため、代表的なものを取り除きます。 |

## 必要なもの

- Python 3.11以上と[uv](https://docs.astral.sh/uv/)
- Google Chrome（Playwright同梱のChromiumでも動きます）
- 外部から使う場合は、Cloudflareのアカウントと、CloudflareでDNSを管理しているドメイン

Windowsで動作を確認しています。macOSとLinuxでも動く想定ですが、未検証です。

## クイックスタート（PC上で試す）

```sh
git clone <このリポジトリのURL>
cd liteproxy
uv sync
cp config.example.toml config.toml
uv run python -m liteproxy
```

`http://127.0.0.1:8700/` を開くとアドレス欄が表示されます。URLを入力するとそのページを、それ以外の語を入力するとDuckDuckGoの検索結果を表示します。

PCのブラウザで確かめるときは、開発者ツールのデバイスエミュレーション（スマホ表示）を使うと、スマートフォンに近い条件になります。

Chromeをインストールしていない場合は、`config.toml` の `[browser]` を `channel = ""` にしてから、`uv run playwright install chromium` を実行してください。

## スマートフォンから使う（Cloudflare Tunnel + Access）

> [!IMPORTANT]
> **Accessの設定を済ませてから、Tunnelでホスト名を公開してください。** 認証のないまま公開すると、誰でも使えるプロキシ（オープンプロキシ）になり、悪用される危険があります。

画面の名称は2026年9月時点のCloudflareダッシュボードのものです。以下では、公開するホスト名を `lite.example.com` とします。

1. **ログイン方法を用意する**
   Zero Trust > Integrations > Identity providers で One-time PIN を追加します（すでにあれば不要です）。
2. **自分だけを許可するポリシーを作る**
   Zero Trust > Access controls > Policies で、Action を Allow にし、Include のセレクタ Emails に自分のメールアドレスを指定したポリシーを作ります。
3. **Accessのアプリケーションを登録する**
   Zero Trust > Access controls > Applications で Create new application > Self-hosted and private を選び、`lite.example.com` と手順2のポリシーを指定します。
   ログイン画面の読み込みにも通信量がかかるため、Session Duration を長め（例：1 month）にし、Apply instant authentication をオンにすることを勧めます。
   作成後、Configure > Additional settings の Application Audience (AUD) Tag を控えます。
4. **Tunnelを作成する**
   Networking > Tunnels で Cloudflared 型のトンネルを作成し、表示される `cloudflared service install <TOKEN>` をPCで実行します。cloudflaredがOSのサービスとして登録され、PCの起動時に自動で接続します。
5. **ホスト名を公開する**
   トンネルの Routes タブで Add route > Published application を選び、`lite.example.com` の Service URL を `http://127.0.0.1:8700` にします。
   `localhost` ではなく `127.0.0.1` と書いてください。liteproxyはIPv4でしか待ち受けないため、`localhost`だとIPv6の`::1`に接続しようとして失敗することがあります。
6. **liteproxy側でもJWTを検証する**
   `config.toml` を次のように設定し、liteproxyを再起動します。
   ```toml
   [access]
   enabled = true
   team_domain = "<チーム名>.cloudflareaccess.com"  # Zero Trust > Settings で確認できる。https:// は付けない
   aud = "<手順3の AUD タグ>"
   allowed_emails = ["you@example.com"]
   ```
   有効にすると、PCから直接 `http://127.0.0.1:8700/` を開いたときは403になります。Cloudflareを経由しない要求を拒否しているためで、正常な動作です。

### うまくつながらないとき

| 症状 | 原因と対処 |
|---|---|
| Cloudflareの502エラー | cloudflaredがliteproxyに接続できていません。まずliteproxyが起動しているか確かめてください。起動しているのに502になる場合は、PCで `http://127.0.0.1:20241/config`（cloudflaredのメトリクス用ページ）を開き、`service` の転送先（ポート番号など）が正しいか確かめてください。 |
| ログイン後に403 | `config.toml` の `aud` と `team_domain` がAccessのアプリケーションの値と一致しているか、`allowed_emails` にログインしたメールアドレスが含まれているかを確かめてください。 |

## 常駐させる

cloudflaredは手順4でサービスになるので、自動で起動する設定が必要なのはliteproxyだけです。PCの起動時やログオン時に、次のコマンドをリポジトリのディレクトリで実行するよう設定してください。

```sh
uv run --directory <リポジトリのパス> python -m liteproxy
```

Windowsでタスクスケジューラに登録する例（管理者のPowerShellで実行します）。`uv.exe` の代わりに、同じ場所にある `uvw.exe` を使うと、コンソールウィンドウを出さずにバックグラウンドで動かせます。

```powershell
$action = New-ScheduledTaskAction -Execute "<uvw.exe のパス>" -Argument 'run --directory "<リポジトリのパス>" python -m liteproxy' -WorkingDirectory "<リポジトリのパス>"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "liteproxy" -Action $action -Trigger $trigger
```

この例の設定では、**Windowsにサインインしたとき**にliteproxyが起動します（cloudflaredはサービスのため、PCの起動時から動きます）。サインインせずロック画面のままの状態でも使いたい場合は、管理者のPowerShellで次のように登録し直してください。PCの起動時に、サインインしていなくても起動します。

```powershell
$action = New-ScheduledTaskAction -Execute "<uvw.exe のパス>" -Argument 'run --directory "<リポジトリのパス>" python -m liteproxy' -WorkingDirectory "<リポジトリのパス>"
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName "liteproxy" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal -Force
```

Linuxではsystemdのユーザーサービス、macOSではlaunchdを使うと、同じように常駐させられます。PCがスリープしている間は使えない点に注意してください。

バックグラウンドで動かしているときの動作ログは、`logs/liteproxy.log` で確認できます。

コードや設定を更新したときは、liteproxyを起動し直してください。Windowsで上の例のようにタスクとして登録している場合は、次のコマンドを実行します（管理者権限は不要です）。ポートを変えている場合は、`8700` を `config.toml` の `[server] port` の値に置き換えてください。

```powershell
Get-NetTCPConnection -LocalPort 8700 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess }
Start-ScheduledTask -TaskName liteproxy
```

1行目で動いているliteproxyを止め、2行目で起動し直します。1行目は、liteproxyがすでに止まっていてもエラーになりません。

## 使い方

- **アドレス欄**：URLを入力するとそのページを開き、それ以外の語を入力するとDuckDuckGoで検索します。
- **読み込み中画面**：PCが描画している間は、処理中の段階（ページを取得、スクリプトの実行を待機、ページ全体を読み込み、軽量化、スマホへ転送）と経過時間を表示します。
  - PCからは、段階の変化とあわせて2秒ごとに生存通知が届きます。
  - 通知が途切れると警告を表示するので、PC側の処理中なのか、通信の遅延なのかを見分けられます。
  - 最後の「スマホへ転送」では、送信量の目安も表示します。
  - 同じページを5分以内にもう一度開いた場合は、PCでの描画を省いてすぐに表示します。
- **画像**：画像の枠をタップすると、その画像だけを読み込みます。
  - 画質は4段階です。

    | 画質 | 内容 |
    |---|---|
    | 低 | 長辺320pxのWebP |
    | 中 | 長辺640pxのWebP（既定） |
    | 高 | 画面の実ピクセル幅までのWebP |
    | 原本 | 元の画像そのまま |

  - 変換するとかえって大きくなる画像（小さなアイコンや、元から軽量な形式の画像）は、元の画像のまま送ります。
  - 長押し（PCでは右クリック）すると、その画像の画質を選んで読み込めます。
  - リンクの中にある画像は、1回目のタップで読み込み、読み込んだあとのタップでリンク先へ移動します。
  - ツールバーの「画像」から、ページ内の画像をまとめて読み込んだり、既定の画質を変えたりできます。既定の画質はCookie（`lp_q`）に保存されます。
- **JavaScriptで動くUI**：タップすると、その要素が点滅し、ツールバーに「PCで操作中… ◯秒」と表示されます。PC側での操作が終わると、画面の変化した部分が更新されます。
  - 通常のリンクはこれまでどおり、liteproxy経由でページを移動します。
  - 開閉（`<details>`）やチェックボックスなど、JavaScriptがなくても動くものは、スマホ側ですぐに反応したうえで、PC側にも同じ操作を伝えます。
  - PC側のページは、操作がないまま10分経つか、同時に保持する数（既定で3ページ）を超えると破棄されます。その後に操作した場合は、通知を表示してから描画し直します。
- **フォーム**：検索欄などのGETフォームは、liteproxy経由で送信先を開きます。ログインや投稿などのPOSTフォームは、入力した値をPC側のページのフォームへ入れてから、PC側で送信します。
  - サイトがフォームに埋め込んでいるトークン（CSRF対策の値）や、PC側のCookieをそのまま使えます。
  - 送信時にサイトのJavaScriptが動く場合も、そのまま動きます。
  - PC側のページが保持されていない場合（保持期限切れなど）は、送信せずに案内を表示します。ページを開き直してから送信してください。
- **表示モード**：ツールバーの「本文」を押すと、本文だけの表示（readerモード）に切り替わります。戻すときは「全体」を押します。選んだモードはCookie（`lp_m`）に保存され、次のページにも適用されます。
  - 一覧ページなど本文が取り出せないページでは、自動で元の表示（layoutモード）に戻ります。
  - readerモードでも、画像は枠のまま表示され、タップで読み込めます。
- **ツールバー**：各ページの上部に次の項目が表示されます。
  - `LP`：ホームに戻る
  - ページタイトル
  - 転送量：`スマホへの送信量（gzipの目安） / PC側の取得量`
  - `画像`：まとめて読み込む、既定の画質を変える
  - `本文` / `全体`：表示モードを切り替える
  - `元`：元のページを直接開く（通信量に注意）
- **画面の条件**：スマートフォンの画面幅、DPR、ダークモードの設定は、liteproxyのページを開いたときにCookie（`lp_env`）へ保存されます。次のページからは、PC側も同じ条件で描画します。

## 設定

設定は `config.toml` に書きます。すべての項目と既定値は [config.example.toml](config.example.toml) にあります。

| セクション | 内容 |
|---|---|
| `[server]` | 待ち受けるアドレスとポート（既定：`127.0.0.1:8700`） |
| `[access]` | Cloudflare AccessのJWTの検証 |
| `[browser]` | 使うブラウザ、同時に描画する数、待ち時間 |
| `[render]` | 既定の表示モード、インラインSVGとdata URIの扱い、class名の削減、取り除く要素 |
| `[session]` | PC側でページを保持するか、保持する数と時間、操作後に変化を待つ時間 |
| `[image]` | 既定の画質、描画時にサイズを計算するか、画像を保持するメモリの上限 |
| `[network]` | PC側でも取得しないドメイン（広告など）、LAN宛てを許可するか |
| `[search]` | 検索に使うURL |
| `[log]` | 動作ログ（既定：`logs/liteproxy.log`）と、転送量を記録するファイル（既定：`logs/stats.jsonl`） |

## セキュリティ

- liteproxyは既定で `127.0.0.1` だけで待ち受けます。外部からは、Cloudflare Tunnelを経由しなければ届きません。
- Cloudflare Accessで利用者を限定し、liteproxy側でもAccessのJWTを検証します。
- LANやループバック宛てのURLは、ページとサブリソースのどちらも取得を拒否します（SSRF対策）。ただし、DNSリバインディングまでは防げません。Accessで利用者を限定することが前提です。
- PC側のChromeは、表示するサイトのJavaScriptを実行します。ページごとに独立したブラウザコンテキストを使い、保持をやめるときに破棄します。
- PC側のChromeでは、ページの写しを作るため、表示するサイト自身のContent-Security-Policyを無効にしています。スマホ側へ返すページには、liteproxyのContent-Security-Policyを付けます。
- 操作の中継（`/a`）は、liteproxyのページのJavaScriptだけが付ける独自のヘッダーを必須にし、他のサイトからの送信では操作できないようにしています。
- 入力済みのパスワード欄の値は、出力するHTMLに含めません。
- 画像の配信（`/i`）では、`image/` で始まる種類の内容だけを返し、`Content-Security-Policy: sandbox` を付けます。画像として取得したSVGにスクリプトが含まれていても、liteproxyのドメイン上では実行されません。

## 制限事項

- JavaScriptで動くUIの操作は、タップ（クリック）だけを中継します。次のものには対応していません。
  - 文字の入力に反応するもの（入力中に候補を出す検索欄など）
  - スワイプ・ドラッグ・ホバーで動くもの（地図、スライダー、カーソルを重ねると開くメニューなど）
  - canvasへの描画、シャドウDOMの内側の変化
- タップのたびにPCとの往復が発生するため、反応までに0.5〜数秒かかります。
- 操作で新たに必要になったCSSは、既存のCSSの後ろに追加します。元のCSSの順序に依存する指定の一部は、元のページと異なる表示になることがあります。
- 動画は、今のところ再生できません。
- CSSの背景画像は取り除かれ、タップしても読み込めません。
- ファイルの添付（アップロード）には対応していません。POSTフォームのほかの項目だけが送信されます。
- readerモードでは、JavaScriptで動くUIの操作はできません（操作したい場合は「全体」に切り替えてください）。SVG形式の画像も落ちます。
- Webフォントを送らないため、文字は端末の標準フォントで表示され、文字幅が少し変わります。アイコンフォントは表示されません。
- Chromeが解釈しないCSS（Safari専用の指定など）は、変換の過程で失われます。

## ロードマップ

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | layoutモード、検索の中継、Tunnel + Access、SSRF対策、転送量の記録 | 完了 |
| 2 | 画像のタップ読み込み、画質の選択、サイズ表示 | 完了 |
| 3 | JavaScriptで動くUIへの対応（PC側で実行し、DOMの差分だけを送る） | 完了 |
| 4 | 動画（低ビットレートへの変換、音声のみなど） | 予定 |
| 5 | readerモード（本文だけの表示） | 完了 |
| 6 | 無限スクロール、入力中に候補を出す検索欄への対応 | 予定 |
| — | Webアプリ（PWA）化 | 検討中 |

## 開発

```sh
uv sync
uv run pytest
```

`tests/test_transform.py`（ローカルのテストページの変換）、`tests/test_client.py`（スマホ側のJSの操作）、`tests/test_session.py`（PC側での操作の再現）、`tests/test_e2e.py`（サーバーを起動し、スマホ役のブラウザから操作する）は、インストール済みのChromeを実際に動かします。Playwright同梱のChromiumを使う場合は、環境変数 `LITEPROXY_TEST_CHANNEL=""` を指定してください。

```
liteproxy/
├─ __main__.py            起動処理（python -m liteproxy）
├─ main.py                FastAPI アプリ（ルーティング・CSP・Access の検証・読み込み中画面の配信・操作の中継）
├─ jobs.py                描画ジョブの管理（進捗の通知、結果の短期保持）
├─ config.py              設定ファイルの読み込み
├─ security.py            SSRF 対策、Access の JWT 検証
├─ urls.py                中継用 URL、入力・フォーム送信の解釈
├─ stats.py               転送量の計測と記録
├─ templates.py           liteproxy 自身のページ（ホーム・エラー・ツールバー・読み込み中画面）
├─ media/
│  └─ image.py            画像の縮小・再圧縮と、元画像・変換結果の保持
├─ static/
│  └─ client.js           スマホ側で動く唯一の JS（画像のタップ読み込み、操作の中継と差分の反映）
├─ browser/
│  ├─ pool.py             Playwright による描画と、保持したページでの操作の再現
│  └─ inject/transform.js PC 側の Chrome で実行する変換処理（ミラーの管理と差分の生成を含む）
└─ render/
   ├─ layout.py           layout モードの仕上げ（ツールバーの差し込み）
   └─ reader.py           reader モード（本文の抽出と最小限の HTML 化）
```

## ライセンス

MIT License です。詳細は [LICENSE](LICENSE) を参照してください。
