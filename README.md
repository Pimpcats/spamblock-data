# spamblock-data

Builds the known-spam list for the SpamBlock iPhone app from the FTC's Do Not
Call complaint data. The list is published on this repository's `data` branch,
where the app downloads it.

## How it works

- Every hour, GitHub Actions runs `ftc_spamlist.py`.
- It downloads each day's complaints from the
  [FTC API](https://github.com/FederalTradeCommission/ftc-api-docs/blob/master/docs/endpoint-dnc-complaints.md)
  and stores how often each number was reported, one file per day.
- A number makes the list when it's reported at least **3 times in the last
  90 days**. Complaints aren't verified and robocallers spoof other people's
  numbers, so one report isn't enough.
- The API returns 50 complaints per request, and a free key allows about
  1,000 requests an hour. The first 90 days therefore take the better part of
  a day to download, newest first. Until then, the list covers the days
  fetched so far.
- Once caught up, the first run each day refreshes the last 7 days, since the
  FTC posts weekend and holiday complaints late. The other runs do nothing.
- Each run fetches for up to 50 minutes, waiting out the key's hourly limit
  when it hits it, then publishes. A run that stops with days still to fetch
  starts the next one itself, because GitHub's hourly schedule can't be
  counted on to. Each run picks up exactly where the last one stopped, even
  partway through a day.
- If a new list would be less than half the size of the current one, it isn't
  published. That usually means an FTC outage, not fewer spammers.

## Setup

1. Get a free API key at https://api.data.gov/signup. The FTC's
   [developer page](https://www.ftc.gov/developer) uses the same form. The key
   arrives by email.
2. In this repository on GitHub, go to **Settings → Secrets and variables →
   Actions → New repository secret**. Name it `FTC_API_KEY` and paste the key
   as the value. Don't put the key anywhere else.
3. Go to **Actions → Update spam list → Run workflow**, tick **smoke**, and
   run it. In the log, the "Smoke-test the FTC API" step should show a page
   of yesterday's complaints. If it says `FTC_API_KEY not set`, the secret
   is missing or misnamed.
4. Run it again with **smoke** unticked, or wait for the next hourly run.

## Cost

Nothing:

- GitHub Actions is free for public repositories on GitHub's standard runners.
- The FTC API key is free.
- `raw.githubusercontent.com` serves the list for free. That's fine for you and
  a few friends. For an App Store release with many users, copy the list to a
  CDN such as Cloudflare R2, which is also free at that size.

## Changing the rules

The settings are constants at the top of `ftc_spamlist.py`:

| Setting | Default | What it does |
| --- | --- | --- |
| `MIN_REPORTS` | 3 | Reports needed to make the list. Raise it if real businesses get blocked; lower it for a bigger list. |
| `WINDOW_DAYS` | 90 | How far back reports count. Raising it downloads the extra days over the next few runs. |

A change that shrinks the list by more than half is blocked by the size check.
Run the workflow with **force** ticked to publish it anyway.

## Troubleshooting

- **"FTC API rejected the key (403)"**: the secret's value is wrong. Replace it.
- **"Refusing to publish N numbers over the previous M"**: the list shrank by
  more than half. If you expected that, run the workflow with **force**.
  Otherwise the FTC probably had an outage; later runs refetch recent days.
- **Updates stopped**: GitHub pauses scheduled workflows in public repositories
  after 60 days without activity. Open **Actions → Update spam list** and
  click **Enable workflow**.
- **A run stopped early**: that's by design. Rate limits and the per-run caps
  end a run cleanly, and the next run continues.

## Running it yourself

Needs Python 3 and nothing else.

```sh
python3 -m unittest -v test_ftc_spamlist
FTC_API_KEY=your-key python3 ftc_spamlist.py --smoke
FTC_API_KEY=your-key python3 ftc_spamlist.py --data-dir data
```
