# Restock Bot

Checks the product pages in `watchlist.yaml` every 15 minutes on GitHub's servers (no laptop needed)
and posts to Discord when something comes **back in stock**, with:

- the product link
- the store's price vs. MSRP (flags third-party sellers charging over retail)
- what it's going for on eBay (optional)

Works for anything with a product page: cards, games, shoes, consoles, etc.

---

## One-time setup (about 10 minutes)

### 1. Make the repo
1. On github.com, tap **+** → **New repository**. Name it `restock-bot`, choose **Public**, then **Create repository**.
2. Tap **uploading an existing file** and drag in everything from this folder. The `.github` folder is hidden on a Mac, so press **Cmd + Shift + .** in Finder to show it.
3. Tap **Commit changes**.

### 2. Add your Discord webhook as a secret
1. In Discord: Server Settings → Integrations → Webhooks → **New Webhook**, pick the channel, then **Copy Webhook URL**.
2. On GitHub: repo **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
3. Name: `DISCORD_WEBHOOK_URL`. Value: paste the URL. Save.

The secret is never shown publicly, even though the repo is public.

### 3. Turn it on and test
1. Open the **Actions** tab and tap **I understand my workflows, go ahead and enable them** if asked.
2. Tap **Restock check** → **Run workflow** → tick **Just send a test alert** → **Run workflow**.
3. A 🧪 test message should show up in Discord within about 2 minutes.
4. Run it again without the box ticked. You'll get a "👀 Now watching" message for every item.

After that it runs by itself every 15 minutes.

### 4. Let your friends add items
Repo **Settings** → **Collaborators** → **Add people** → their GitHub usernames.
They can then edit `watchlist.yaml` from the GitHub app or website.

---

## Adding or removing items
Edit `watchlist.yaml` (instructions are at the top of the file). The bot checks right away
whenever the file changes and confirms in Discord. If someone makes a typo, the bot
posts which line is wrong and keeps watching everything else.

## eBay price comparison (optional, free)
1. Sign up at developer.ebay.com and create a **Production** keyset.
2. Add two more repo secrets: `EBAY_CLIENT_ID` (the App ID) and `EBAY_CLIENT_SECRET` (the Cert ID).

Alerts will then show the lowest and median eBay asking price for new, buy-it-now listings.
These are asking prices, not sold prices.

## Running it on your Mac instead
```
pip3 install -r requirements.txt
python3 -m playwright install chromium
echo 'DISCORD_WEBHOOK_URL=paste-your-url-here' > .env
python3 monitor.py --loop 60
```
`.env` is never uploaded to GitHub (see `.gitignore`).

## Good to know
- **Cost:** free, as long as the repo stays public and uses the standard runner (already set).
- **Blocked stores:** some stores (Walmart especially) show a "robot or human?" check to cloud servers.
  The bot detects this, tells Discord once after 3 hours of failures, and keeps trying quietly.
- **Wrong status for an item?** Add the exact button words for that item:
  ```yaml
  - name: Something
    url: https://...
    in_stock_phrases: ["add to cart"]
    out_of_stock_phrases: ["sold out"]
  ```
