# Arknights CN → Discord news bot (free starter)

Checks the Arknights CN official homepage, the official Weibo account, and the official Bilibili account every 30 minutes. Sends links, available images, original Chinese text, and best-effort free automatic English translations to a Discord channel.

**Sources:**
- Official site: https://ak.hypergryph.com/ (the three most recent homepage news links)
- Weibo: https://weibo.com/arknights (UID 6279793937). If the mobile API rejects requests, falls back to the account's Sina mirror at https://sina.cn/media/6279793937 . Mirror items link to Sina's copy, not to the original Weibo post.
- Bilibili: https://space.bilibili.com/161775300 (public dynamic API; sometimes requires authentication or rejects cloud IPs)

**Limitations:** This is a best-effort monitor, not a guaranteed real-time official API. The homepage only displays a few news items, and Weibo/Bilibili can block automated access. It does not scrape every operator image or all content of an article. GoogleTranslator is a free *unofficial* translation method; it can fail or produce imperfect translations, in which case original Chinese is sent. Every source tracks its own post IDs; the same announcement posted on two platforms may generate two messages. GitHub schedule runs may be late or skipped under load. No paid subscriptions are required within included GitHub Actions allowance.

## 1. Create a Discord webhook

1. Create a Discord text channel, e.g. `#arknights-cn-news`.
2. Channel Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
3. Treat this URL like a password. **Never paste it in a public repository, commit it into a file, or share it in chat.**

## 2. Put this folder into GitHub

1. Make a **public** GitHub repository, e.g. `arknights-cn-news-bot`, without creating a starter README on GitHub.
2. Extract this ZIP into a folder on your Windows PC. Make sure the hidden `.github/workflows/check-news.yml` file is present.
3. Open PowerShell in the extracted `arknights-cn-discord-bot` folder. With Git for Windows installed, run:

```powershell
git init -b main
git add .
git commit -m "Initial Arknights CN bot"
git remote add origin https://github.com/YOUR_USERNAME/arknights-cn-news-bot.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your actual GitHub username. Authenticate with GitHub when prompted; GitHub account passwords do not work as Git-over-HTTPS passwords. The alternative is to upload the extracted files through the GitHub website, preserving the `.github/workflows` directory.

## 3. Store the Discord webhook safely

In your GitHub repository open **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `DISCORD_WEBHOOK_URL`
- Value: your full Discord webhook URL

Don't put it in `bot.py`, `state.json`, or the workflow file.

## 4. Test from GitHub without waiting for a new announcement

1. Open **Actions → Arknights CN news to Discord → Run workflow**.
2. Check **Send a connection test instead of fetching news** and run it.
3. A test message should appear in your Discord channel.
4. Run again with the checkbox **unchecked** to establish initial seen-post history. Existing posts won't send notifications.
5. Open the job logs. Look for a success line for each feed. A blocked source logs an error but the other feeds can still work.
6. Subsequent runs happen around `:17` and `:47` each hour, in UTC. The schedule can be delayed or skipped.

**Important:** The bot stores seen posts in `state.json`, committed by GitHub Actions. If your repository/org's Actions workflow-token permissions are restricted to read-only, update **Settings → Actions → General → Workflow permissions** to allow read/write. The workflow itself requests `contents: write`. A **public** repository is the simplest route to $0 GitHub-hosted Actions minutes. Your webhook stays private in a repository secret; only the code and seen-post IDs are public. You can use a private repository instead, but its Actions minutes are metered against your account's included allowance.

## Optional local test (Windows PowerShell)

Use Python 3.12+ and run in the project folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DISCORD_WEBHOOK_URL = Read-Host "Paste your private Discord webhook URL"
.\.venv\Scripts\python.exe bot.py --test
.\.venv\Scripts\python.exe bot.py --dry-run
```

`--test` sends one connection message; `--dry-run` checks the feeds without sending any news or saving state. For a real local run, use `python bot.py` (without `--dry-run`).

## Running tests without network calls

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Troubleshooting

- **No Discord test message:** Check `DISCORD_WEBHOOK_URL` secret and the Actions job's error log.
- **Run worked but no news:** First successful poll only records existing post IDs. Wait for a *new* post.
- **Weibo API error:** Bot automatically tries Sina's public mirror. If both are blocked, the log will say so; it will retry next run.
- **Bilibili 412 / -352:** Anti-bot restrictions. This free approach cannot guarantee cloud access; do not paste account cookies or passwords into workflow files.
- **State not saved:** Look at the final workflow step; make sure `contents: write` permission is available. Without a successful state commit, duplicate messages may occur on future runs.
- **GitHub Actions schedule stops:** Check the Actions tab. Scheduled workflows can be disabled after 60 inactive days in public repositories; keep the repo active as appropriate.
- **Want more detailed news/character cards?** Version 2 can improve source-specific parsing, translation quality, categorization, and image extraction after confirming which sources are reachable from GitHub.
