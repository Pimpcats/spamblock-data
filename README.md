# SpamBlock known-spam list

This branch is rebuilt automatically from the FTC's Do Not Call complaint data
by the `Update spam list` workflow on `main`. Don't edit it: every update
replaces the whole branch.

| File | What it holds |
| --- | --- |
| `spam-numbers.txt` | Numbers reported to the FTC at least 3 times in the last 90 days. One 10-digit North American number per line, ascending, no duplicates. |
| `spam-numbers.json` | How many numbers, the list's SHA-256, when it was built, and which days of complaints it covers. |
| `days/` | Complaint counts per number for each day. Kept so each update only fetches what's new. |
| `partial/` | Where a run stopped partway through a day, so the next run picks up from there. Usually empty. |

Download links:

- https://raw.githubusercontent.com/Pimpcats/spamblock-data/data/spam-numbers.txt
- https://raw.githubusercontent.com/Pimpcats/spamblock-data/data/spam-numbers.json

Check the list against `sha256` in the JSON before using it. The two files
are cached separately, so right after an update they can briefly disagree;
if they do, try again later.

## Source and caveats

The data comes from the FTC's
[Do Not Call Reported Calls Data API](https://github.com/FederalTradeCommission/ftc-api-docs/blob/master/docs/endpoint-dnc-complaints.md).
Consumers report these numbers and the FTC doesn't verify them. Robocallers
often spoof numbers that belong to someone else, so one report isn't enough
to get a number on the list.
