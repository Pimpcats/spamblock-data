#!/usr/bin/env python3
"""Builds SpamBlock's known-spam list from FTC Do Not Call complaint data.

The FTC publishes the phone number behind every Do Not Call and robocall
complaint, each weekday. This keeps a rolling window of those complaints and
publishes the numbers reported repeatedly — repeated because the data is
unverified and callers spoof numbers, so a single report says little and
blocking on one would catch innocent people.

Each run:
  1. fetches the complaint days that are missing or not yet final, newest
     first, within a per-run request budget;
  2. stores each finished day as number/count pairs under days/;
  3. sums the last WINDOW_DAYS of days and keeps numbers reported at least
     MIN_REPORTS times;
  4. writes spam-numbers.txt (ascending, one 10-digit number per line) and
     spam-numbers.json (count, checksum, provenance).

The FTC API returns at most 50 records per request, and a free api.data.gov
key allows about 1,000 requests an hour, so a 90-day window cannot be fetched
in one go. Storing finished days and fetching only what is missing turns the
first backfill into a series of hourly runs and every later run into a small
top-up.

Standard library only, so the workflow installs nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

API_URL = "https://api.ftc.gov/v0/dnc-complaints"
PAGE_SIZE = 50                 # the API's maximum
WINDOW_DAYS = 90               # how far back reports count
MIN_REPORTS = 3                # reports needed within the window
FRESH_DAYS = 7                 # recent days re-fetched daily; covers weekends
                               # and holiday closures, when the FTC posts late
REFETCH_AFTER_HOURS = 20       # "daily", with slack for schedule drift
REQUEST_BUDGET = 800           # per run; the key allows ~1,000 an hour
RUN_MINUTES = 30               # stop fetching after this long; the next run
                               # continues, and the job never hits its timeout
MAX_SHRINK = 0.5               # refuse to publish a list this much smaller

SERVICE_CODES = {211, 311, 411, 511, 611, 711, 811, 911}

LIST_FILE = "spam-numbers.txt"
META_FILE = "spam-numbers.json"
DAYS_DIR = "days"
PARTIAL_DIR = "partial"        # days a run stopped partway through


# ─── Numbers ────────────────────────────────────────────────────────────────

def normalize(raw: str | None) -> str | None:
    """The 10-digit North American number in a complaint, or None.

    Complaints hold whatever the consumer typed. Anything that isn't a
    dialable +1 number — empty, too short, a service code, an exchange that
    can't be assigned — is dropped rather than guessed at.
    """
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isascii() and c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    npa, nxx = int(digits[:3]), int(digits[3:6])
    if not (200 <= npa <= 999) or npa in SERVICE_CODES:
        return None
    if not (200 <= nxx <= 999) or nxx in SERVICE_CODES:
        return None
    return digits


# ─── Fetching ───────────────────────────────────────────────────────────────

class BudgetExhausted(Exception):
    """The run has used its request allowance; later runs carry on."""


class RateLimited(BudgetExhausted):
    """The API key's hourly allowance is spent.

    Handled exactly like an exhausted budget — stop, keep finished days, let
    the next hourly run continue — because retrying within the same hour
    cannot succeed.
    """


class PagingStalled(Exception):
    """The API returned a full page of records already seen."""


class Fetcher:
    """Pages through the FTC API for one day at a time.

    `get_json` and `clock` are injected so tests can stand in for the network
    and for time. `deadline` is a `clock()` value after which no new request
    starts.
    """

    def __init__(self, get_json: Callable[[str], dict], budget: int = REQUEST_BUDGET,
                 deadline: float | None = None, clock: Callable[[], float] = time.monotonic):
        self.get_json = get_json
        self.remaining = budget
        self.deadline = deadline
        self.clock = clock
        self.requests = 0

    def _request(self, params: dict) -> dict:
        if self.remaining <= 0:
            raise BudgetExhausted()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise BudgetExhausted()
        self.remaining -= 1
        self.requests += 1
        # Date values must be wrapped in double quotes, per the API docs.
        return self.get_json(API_URL + "?" + urllib.parse.urlencode(params))

    @staticmethod
    def _total(page: dict) -> int | None:
        """The record count the API reports, or None if it doesn't report one.

        The docs show "record-total" in the example and "records-total" in the
        field table. The live API sends "record-total" holding a stray
        complaint record instead of a count, so anything that isn't a number
        counts as unknown.
        """
        meta = page.get("meta") or {}
        for key in ("record-total", "records-total"):
            value = meta.get(key)
            if isinstance(value, (int, str)) and str(value).strip().isdigit():
                return int(value)
        return None

    def fetch_window(self, start: dt.datetime, end: dt.datetime,
                     progress: dict | None = None) -> dict[str, str | None]:
        """Complaint id -> normalised number for [start, end], inclusive.

        Pages until a page comes back short: the live API doesn't report a
        total. `progress` ({"offset", "found"}) is updated after every page,
        so when the budget runs out partway, the caller can save it and pass
        it back in later to carry on from there. Keyed by complaint id, so
        records seen twice across a resume are counted once.
        """
        fmt = "%Y-%m-%d %H:%M:%S"
        base = {
            "created_date_from": f'"{start.strftime(fmt)}"',
            "created_date_to": f'"{end.strftime(fmt)}"',
            "items_per_page": PAGE_SIZE,
        }
        if progress is None:
            progress = {"offset": 0, "found": {}}
        found = progress["found"]
        while True:
            page = self._request({**base, "offset": progress["offset"]})
            records = page.get("data") or []
            before = len(found)
            for record in records:
                attributes = record.get("attributes") or {}
                key = record.get("id") or attributes.get("seq") or f"{progress['offset']}:{len(found)}"
                found[str(key)] = normalize(attributes.get("company-phone-number"))
            progress["offset"] += len(records)
            if len(records) < PAGE_SIZE:
                return found
            # A full page of records already seen means the offset isn't
            # moving through the results; stop rather than loop until the
            # budget runs out.
            if len(found) == before:
                raise PagingStalled(f"offset {progress['offset']} returned only records already seen")

    def fetch_day(self, day: dt.date, progress: dict | None = None) -> dict[str, int]:
        """Number -> complaint count for complaints created on `day`."""
        start = dt.datetime.combine(day, dt.time(0, 0, 0))
        end = dt.datetime.combine(day, dt.time(23, 59, 59))
        counts: dict[str, int] = {}
        for number in self.fetch_window(start, end, progress).values():
            if number:
                counts[number] = counts.get(number, 0) + 1
        return counts


def http_get_json(api_key: str) -> Callable[[str], dict]:
    """A get_json for the real API, retrying server and network errors."""
    repository = os.environ.get("GITHUB_REPOSITORY", "Pimpcats/spamblock-data")

    def get(url: str) -> dict:
        request = urllib.request.Request(url, headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": f"spamblock-data (+https://github.com/{repository})",
        })
        delay = 5.0
        for attempt in range(6):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    raise RateLimited() from error
                if error.code in (500, 502, 503, 504) and attempt < 5:
                    wait = float(error.headers.get("Retry-After") or delay)
                    time.sleep(min(wait, 120))
                    delay *= 2
                    continue
                if error.code == 403:
                    sys.exit("FTC API rejected the key (403). Check the FTC_API_KEY secret.")
                raise
            except urllib.error.URLError:
                if attempt < 5:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("unreachable")
    return get


# ─── Storage ────────────────────────────────────────────────────────────────

def day_path(data_dir: Path, day: dt.date) -> Path:
    return data_dir / DAYS_DIR / f"{day.isoformat()}.json"


def load_day(data_dir: Path, day: dt.date) -> dict | None:
    path = day_path(data_dir, day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_day(data_dir: Path, day: dt.date, counts: dict[str, int], now: dt.datetime) -> None:
    path = day_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "date": day.isoformat(),
        "fetched_at": now.isoformat(timespec="seconds"),
        "complaints": sum(counts.values()),
        "counts": dict(sorted(counts.items())),
    }
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n")


def partial_path(data_dir: Path, day: dt.date) -> Path:
    return data_dir / PARTIAL_DIR / f"{day.isoformat()}.json"


def load_partial(data_dir: Path, day: dt.date, now: dt.datetime) -> dict:
    """Where an earlier run stopped in `day`, or a fresh start.

    Progress older than REFETCH_AFTER_HOURS is dropped: a recent day can
    gain complaints between runs, and starting over is cheaper than
    reasoning about how the pages moved.
    """
    fresh = {"offset": 0, "found": {}}
    path = partial_path(data_dir, day)
    try:
        saved = json.loads(path.read_text())
        saved_at = dt.datetime.fromisoformat(saved["saved_at"])
        progress = {"offset": int(saved["offset"]), "found": dict(saved["found"])}
    except (OSError, ValueError, KeyError, TypeError):
        return fresh
    if now - saved_at > dt.timedelta(hours=REFETCH_AFTER_HOURS):
        return fresh
    return progress


def save_partial(data_dir: Path, day: dt.date, progress: dict, now: dt.datetime) -> None:
    path = partial_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"date": day.isoformat(), "saved_at": now.isoformat(timespec="seconds"), **progress}
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n")


def days_to_fetch(data_dir: Path, today: dt.date, now: dt.datetime) -> list[dt.date]:
    """Days in the window that are missing, or recent and not refreshed today.

    Newest first, so an interrupted backfill still publishes the most useful
    data. Today itself is skipped: the FTC publishes a day's complaints the
    next weekday.
    """
    wanted = []
    for age in range(1, WINDOW_DAYS + 1):
        day = today - dt.timedelta(days=age)
        stored = load_day(data_dir, day)
        if stored is None:
            wanted.append(day)
        elif age <= FRESH_DAYS:
            fetched = dt.datetime.fromisoformat(stored["fetched_at"])
            if now - fetched > dt.timedelta(hours=REFETCH_AFTER_HOURS):
                wanted.append(day)
    return wanted


def prune(data_dir: Path, today: dt.date) -> list[str]:
    """Delete day files that have aged out of the window (with a week's slack)."""
    removed = []
    oldest = today - dt.timedelta(days=WINDOW_DAYS + 7)
    paths = list((data_dir / DAYS_DIR).glob("*.json")) + list((data_dir / PARTIAL_DIR).glob("*.json"))
    for path in sorted(paths):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < oldest:
            path.unlink()
            removed.append(path.name)
    return removed


# ─── Building the list ──────────────────────────────────────────────────────

def aggregate(data_dir: Path, today: dt.date) -> tuple[dict[str, int], list[dt.date], int]:
    """Summed counts over the window, the days that contributed, and the total."""
    totals: dict[str, int] = {}
    covered = []
    complaints = 0
    for age in range(1, WINDOW_DAYS + 1):
        day = today - dt.timedelta(days=age)
        stored = load_day(data_dir, day)
        if stored is None:
            continue
        covered.append(day)
        complaints += stored.get("complaints", 0)
        for number, count in stored.get("counts", {}).items():
            totals[number] = totals.get(number, 0) + count
    return totals, sorted(covered), complaints


def select(totals: dict[str, int], min_reports: int = MIN_REPORTS) -> list[str]:
    """Numbers reported at least `min_reports` times, ascending."""
    return sorted(n for n, c in totals.items() if c >= min_reports and normalize(n) == n)


def render(numbers: Iterable[str]) -> bytes:
    return "".join(f"{n}\n" for n in numbers).encode("ascii")


def publish(data_dir: Path, numbers: list[str], covered: list[dt.date], complaints: int,
            now: dt.datetime, force: bool = False) -> bool:
    """Writes the list and its metadata. Returns whether anything changed.

    Refuses to replace a list with one less than half its size unless forced:
    an outage or a format change on the FTC side would otherwise publish a
    near-empty list and quietly unblock everything.
    """
    body = render(numbers)
    list_path, meta_path = data_dir / LIST_FILE, data_dir / META_FILE

    previous = list_path.read_bytes() if list_path.exists() else None
    if previous == body:
        return False
    if previous and not force:
        before = previous.count(b"\n")
        if len(numbers) < before * (1 - MAX_SHRINK):
            sys.exit(f"Refusing to publish {len(numbers):,} numbers over the previous "
                     f"{before:,}. Rerun with --force if this is expected.")

    list_path.write_bytes(body)
    meta = {
        "count": len(numbers),
        "sha256": hashlib.sha256(body).hexdigest(),
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": WINDOW_DAYS,
        "min_reports": MIN_REPORTS,
        "days_covered": len(covered),
        "first_day": covered[0].isoformat() if covered else None,
        "last_day": covered[-1].isoformat() if covered else None,
        "complaints_in_window": complaints,
        "source": "FTC Do Not Call Reported Calls Data (api.ftc.gov/v0/dnc-complaints)",
        "note": "Complaint data is reported by consumers and is not verified by the FTC.",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return True


# ─── Checking the live API ──────────────────────────────────────────────────

DATASETS_PAGE = "https://www.ftc.gov/site-information/open-government/data-sets/do-not-call-data"
PROBE_OFFSETS = (1_000, 2_000, 5_000, 10_000, 20_000, 40_000)


def http_get_text(url: str, limit: int = 20_000_000) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (spamblock-data)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(limit).decode("utf-8", "replace")


def smoke(get_json: Callable[[str], dict], day: dt.date,
          get_text: Callable[[str], str] | None = None) -> dict:
    """Shows how the live API behaves, for deciding how to fetch from it.

    One page of `day`, then pages at increasing offsets into the same day:
    the deepest offset that still returns records bounds the day's volume
    (the API reports no count) and shows whether deep offsets work at all.
    With `get_text`, also looks at the FTC's CSV downloads of the same data.
    """
    window = {
        "created_date_from": f'"{day} 00:00:00"',
        "created_date_to": f'"{day} 23:59:59"',
        "items_per_page": PAGE_SIZE,
    }

    def page_at(offset: int) -> dict:
        started = time.monotonic()
        try:
            page = get_json(API_URL + "?" + urllib.parse.urlencode({**window, "offset": offset}))
        except urllib.error.HTTPError as error:
            return {"error": error.code, "seconds": round(time.monotonic() - started, 1)}
        records = page.get("data") or []
        created = [(r.get("attributes") or {}).get("created-date") for r in records]
        return {
            "records": len(records),
            "newest_and_oldest": [created[0], created[-1]] if created else [],
            "seconds": round(time.monotonic() - started, 1),
            "page": page,
        }

    first = page_at(0)
    page = first.pop("page", {})
    records = page.get("data") or []
    report = {
        "day": day.isoformat(),
        "first_page": first,
        "total_as_read": Fetcher._total(page) if page else None,
        "usable_numbers": sum(1 for r in records
                              if normalize((r.get("attributes") or {}).get("company-phone-number"))),
        "sample_attributes": records[0].get("attributes") if records else None,
        "deep_offsets": {},
    }
    for offset in PROBE_OFFSETS:
        result = page_at(offset)
        result.pop("page", None)
        report["deep_offsets"][str(offset)] = result
        if not result.get("records"):
            break
    if get_text:
        report["csv_downloads"] = probe_csv(get_text)
    return report


def probe_csv(get_text: Callable[[str], str]) -> dict:
    """The CSV files linked from the FTC's data-sets page: how many, what
    they're called, and the header and size of the first one."""
    try:
        html = get_text(DATASETS_PAGE)
    except Exception as error:  # a probe reports failures rather than raising
        return {"error": repr(error)[:300]}
    links = list(dict.fromkeys(re.findall(r'href="([^"]+\.csv[^"]*)"', html, re.IGNORECASE)))
    report: dict = {"links": len(links), "first_links": links[:4], "last_links": links[-2:]}
    if links:
        url = urllib.parse.urljoin(DATASETS_PAGE, links[0])
        try:
            lines = get_text(url).splitlines()
            report.update(sample_file=url, lines=len(lines), first_lines=lines[:3])
        except Exception as error:
            report.update(sample_file=url, error=repr(error)[:300])
    return report


# ─── Entry point ────────────────────────────────────────────────────────────

def run(data_dir: Path, fetcher: Fetcher, now: dt.datetime, force: bool = False,
        log: Callable[[str], None] = lambda message: None) -> dict:
    today = now.date()
    wanted = days_to_fetch(data_dir, today, now)
    log(f"{len(wanted)} days to fetch")
    fetched, stopped_early, saved_partial = [], False, False
    for day in wanted:
        progress = load_partial(data_dir, day, now)
        resumed_at = progress["offset"]
        try:
            counts = fetcher.fetch_day(day, progress)
        except BudgetExhausted:
            if progress["offset"] > resumed_at:
                save_partial(data_dir, day, progress, now)
                saved_partial = True
                log(f"{day}: stopped at {progress['offset']:,} complaints; the next run resumes there.")
            log(f"Stopping after {fetcher.requests} requests; the next run continues.")
            stopped_early = True
            break
        save_day(data_dir, day, counts, now)
        partial_path(data_dir, day).unlink(missing_ok=True)
        fetched.append(day)
        log(f"{day}: {sum(counts.values()):,} complaints, {len(counts):,} numbers "
            f"({fetcher.requests} requests so far)")

    removed = prune(data_dir, today)
    totals, covered, complaints = aggregate(data_dir, today)
    numbers = select(totals)
    changed = publish(data_dir, numbers, covered, complaints, now, force) if covered else False
    return {
        "fetched_days": [d.isoformat() for d in fetched],
        "pruned": removed,
        "requests": fetcher.requests,
        "stopped_early": stopped_early,
        "days_covered": len(covered),
        "complaints_in_window": complaints,
        "numbers": len(numbers),
        "changed": changed or bool(fetched) or bool(removed) or saved_partial,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path,
                        help="where days/ and the published list live")
    parser.add_argument("--smoke", action="store_true",
                        help="probe the API and the CSV downloads and print what came back")
    parser.add_argument("--force", action="store_true",
                        help="publish even if the list shrank sharply")
    args = parser.parse_args()
    if args.data_dir is None and not args.smoke:
        parser.error("--data-dir is required")

    api_key = os.environ.get("FTC_API_KEY")
    if not api_key:
        if not args.smoke:
            sys.exit("FTC_API_KEY is not set. Get a free key at https://api.data.gov/signup "
                     "and add it as a repository secret named FTC_API_KEY.")
        # A smoke test is one request, which the shared demo key can manage.
        print("FTC_API_KEY not set; smoke-testing with DEMO_KEY.")
        api_key = "DEMO_KEY"
    get_json = http_get_json(api_key)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    if args.smoke:
        print(json.dumps(smoke(get_json, now.date() - dt.timedelta(days=1), http_get_text), indent=2))
        return

    args.data_dir.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(get_json, deadline=time.monotonic() + RUN_MINUTES * 60)
    summary = run(args.data_dir, fetcher, now, args.force, log=lambda m: print(m, flush=True))
    print(json.dumps(summary, indent=2))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"changed={'true' if summary['changed'] else 'false'}\n")
            f.write(f"numbers={summary['numbers']}\n")


if __name__ == "__main__":
    main()
