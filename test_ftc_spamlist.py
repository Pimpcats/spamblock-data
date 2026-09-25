"""Tests for ftc_spamlist, against a fake FTC API that follows the documented
request and response format, including its paging and quoted-date filters."""

import datetime as dt
import json
import random
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import ftc_spamlist as f


class FakeFTC:
    """Serves complaints the way api.ftc.gov documents: at most 50 per page,
    filtered by quoted created_date_from/to, with pagination metadata."""

    # What the live API puts where the docs promise a count.
    STRAY_RECORD = {"_id": "84e5432fa610264d22f95ec93d1cee03", "seq": 19479399}

    def __init__(self, complaints, total_key="record-total", total="stray", ignore_offset=False):
        # complaints: list of (id, created datetime, raw phone string)
        # total: "stray" as the live API does, "count" as documented, or None
        self.complaints = complaints
        self.total_key = total_key
        self.total = total
        self.ignore_offset = ignore_offset
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        unquote = lambda s: s.strip('"')
        if "created_date" in query:  # a whole day
            day = dt.date.fromisoformat(unquote(query["created_date"]))
            start = dt.datetime.combine(day, dt.time(0, 0, 0))
            end = dt.datetime.combine(day, dt.time(23, 59, 59))
        else:
            start = dt.datetime.fromisoformat(unquote(query["created_date_from"]))
            end = dt.datetime.fromisoformat(unquote(query["created_date_to"]))
        size = min(int(query.get("items_per_page", 50)), 50)
        offset = 0 if self.ignore_offset else int(query.get("offset", 0))
        matching = sorted(
            (c for c in self.complaints if start <= c[1] <= end),
            key=lambda c: c[1], reverse=True,  # the API's default is DESC
        )
        page = matching[offset:offset + size]
        return {
            "data": [{
                "type": "dnc_complaint",
                "id": cid,
                "attributes": {
                    "company-phone-number": phone,
                    "created-date": when.strftime("%Y-%m-%d %H:%M:%S"),
                },
            } for cid, when, phone in page],
            "meta": {"records-this-page": len(page), **self._total(len(matching))},
        }

    def _total(self, count):
        if self.total == "count":
            return {self.total_key: count}
        if self.total == "stray":
            return {self.total_key: self.STRAY_RECORD}
        return {}


NOW = dt.datetime(2026, 9, 24, 18, 30, 0)
TODAY = NOW.date()


def complaint_stream(days=10, per_day=120, seed=1):
    """Complaints over recent days: a few repeat offenders among one-offs."""
    rng = random.Random(seed)
    repeaters = ["9092456175", "9094021609", "2135550188"]
    out, n = [], 0
    for age in range(1, days + 1):
        day = TODAY - dt.timedelta(days=age)
        for i in range(per_day):
            when = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=rng.randrange(86400))
            if i < 3:
                phone = repeaters[i]
            else:
                phone = f"{rng.randrange(200, 999)}{rng.randrange(200, 999)}{rng.randrange(10000):04d}"
            out.append((f"c{n}", when, phone))
            n += 1
    return out


class NormalizeTests(unittest.TestCase):
    def test_accepts_the_formats_people_type(self):
        for raw in ["9092456175", "(909) 245-6175", "1-909-245-6175", "+1 909 245 6175"]:
            self.assertEqual(f.normalize(raw), "9092456175", raw)

    def test_rejects_what_cannot_ring(self):
        for raw in [None, "", "611", "909245617", "44 20 7946 0958",
                    "1092456175",   # area code can't start with 1
                    "9114561234",   # N11 area code
                    "9094111234",   # N11 exchange
                    "9091451234"]:  # exchange can't start with 1
            self.assertIsNone(f.normalize(raw), raw)


class FetchTests(unittest.TestCase):
    def test_pages_through_a_day_exactly_once_per_complaint(self):
        data = complaint_stream(days=1, per_day=137)
        fetcher = f.Fetcher(FakeFTC(data))
        counts = fetcher.fetch_day(TODAY - dt.timedelta(days=1))
        valid = sum(1 for _, _, p in data if f.normalize(p))
        self.assertEqual(sum(counts.values()), valid)
        self.assertEqual(fetcher.requests, 3)  # 50 + 50 + 37

    def test_either_spelling_of_the_total_works(self):
        data = complaint_stream(days=1, per_day=75)
        for key in ("record-total", "records-total"):
            counts = f.Fetcher(FakeFTC(data, total_key=key, total="count")).fetch_day(TODAY - dt.timedelta(days=1))
            self.assertEqual(sum(counts.values()), sum(1 for _, _, p in data if f.normalize(p)), key)

    def test_pages_to_the_end_when_the_total_is_missing_or_junk(self):
        for per_day, pages in [(137, 3), (100, 3), (49, 1), (0, 1)]:
            data = complaint_stream(days=1, per_day=per_day)
            for total in ("stray", None):
                fake = FakeFTC(data, total=total)
                fetcher = f.Fetcher(fake)
                counts = fetcher.fetch_day(TODAY - dt.timedelta(days=1))
                label = f"{per_day} complaints, total={total}"
                self.assertEqual(sum(counts.values()), sum(1 for _, _, p in data if f.normalize(p)), label)
                self.assertEqual(fetcher.requests, pages, label)

    def test_junk_totals_read_as_unknown(self):
        for meta in [{"record-total": FakeFTC.STRAY_RECORD}, {"record-total": None}, {},
                     {"records-this-page": 50}, {"record-total": "many"}]:
            self.assertIsNone(f.Fetcher._total({"meta": meta, "data": [{}] * 50}), meta)
        self.assertEqual(f.Fetcher._total({"meta": {"record-total": "1234"}}), 1234)

    def test_an_api_that_ignores_the_offset_stops_instead_of_looping(self):
        fake = FakeFTC(complaint_stream(days=1, per_day=120), total="stray", ignore_offset=True)
        with self.assertRaises(f.PagingStalled):
            f.Fetcher(fake).fetch_day(TODAY - dt.timedelta(days=1))
        self.assertEqual(len(fake.urls), 2)

    def test_dates_are_sent_in_double_quotes(self):
        fake = FakeFTC([])
        f.Fetcher(fake).fetch_day(TODAY - dt.timedelta(days=1))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.urls[0]).query))
        self.assertTrue(query["created_date_from"].startswith('"'))
        self.assertTrue(query["created_date_to"].endswith('"'))


class SmokeTests(unittest.TestCase):
    def test_probes_deeper_until_the_records_run_out(self):
        fake = FakeFTC(complaint_stream(days=1, per_day=1_500))
        report = f.smoke(fake, TODAY - dt.timedelta(days=1))
        self.assertIsNone(report["total_as_read"])
        self.assertEqual(report["first_page"]["records"], 50)
        self.assertEqual(report["deep_offsets"]["1000"]["records"], 50)
        self.assertEqual(report["deep_offsets"]["2000"]["records"], 0)
        self.assertNotIn("5000", report["deep_offsets"])
        json.dumps(report)  # printable

    def test_reads_the_csv_links(self):
        pages = {
            f.DATASETS_PAGE: '<a href="/system/files/dnc-2026-09-23.csv">x</a> <a href="/b.CSV">y</a>',
            "https://www.ftc.gov/system/files/dnc-2026-09-23.csv": "Company_Phone_Number,Created_Date\n1,2\n",
        }
        report = f.probe_csv(pages.__getitem__)
        self.assertEqual(report["links"], 2)
        self.assertEqual(report["lines"], 2)
        self.assertEqual(report["first_lines"][0], "Company_Phone_Number,Created_Date")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_repeat_offenders_are_listed_and_one_offs_are_not(self):
        summary = f.run(self.dir, f.Fetcher(FakeFTC(complaint_stream(days=10))), NOW)
        listed = (self.dir / f.LIST_FILE).read_text().split()
        self.assertEqual(listed, sorted(["2135550188", "9092456175", "9094021609"]))
        self.assertEqual(summary["numbers"], 3)
        meta = json.loads((self.dir / f.META_FILE).read_text())
        self.assertEqual(meta["count"], 3)
        import hashlib
        self.assertEqual(meta["sha256"], hashlib.sha256((self.dir / f.LIST_FILE).read_bytes()).hexdigest())

    def test_an_exhausted_budget_keeps_finished_days_and_resumes(self):
        data = complaint_stream(days=10, per_day=120)  # 3 requests a day
        first = f.run(self.dir, f.Fetcher(FakeFTC(data), budget=7), NOW)
        self.assertTrue(first["stopped_early"])
        self.assertEqual(len(first["fetched_days"]), 2)       # a third day didn't finish
        self.assertEqual(first["fetched_days"][0], "2026-09-23")  # newest first
        second = f.run(self.dir, f.Fetcher(FakeFTC(data), budget=1000), NOW)
        self.assertFalse(second["stopped_early"])
        # Every day in the window is fetched, including the ones with no data.
        self.assertEqual(second["days_covered"], f.WINDOW_DAYS)

    def test_a_rate_limit_stops_cleanly_and_keeps_finished_days(self):
        fake = FakeFTC(complaint_stream(days=10, per_day=120))
        calls = {"n": 0}
        def limited(url):
            calls["n"] += 1
            if calls["n"] > 7:
                raise f.RateLimited()
            return fake(url)
        summary = f.run(self.dir, f.Fetcher(limited), NOW)
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(len(summary["fetched_days"]), 2)
        self.assertTrue((self.dir / f.LIST_FILE).exists())

    def test_a_time_limit_stops_cleanly_like_a_spent_budget(self):
        ticks = iter(range(1_000))  # one tick per request
        fetcher = f.Fetcher(FakeFTC(complaint_stream(days=10, per_day=120)),
                            deadline=7, clock=lambda: next(ticks))
        summary = f.run(self.dir, fetcher, NOW)
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(summary["requests"], 7)
        self.assertEqual(len(summary["fetched_days"]), 2)

    def test_a_caught_up_run_only_refreshes_recent_days_once_a_day(self):
        data = complaint_stream(days=10, per_day=60)
        f.run(self.dir, f.Fetcher(FakeFTC(data), budget=10_000), NOW)
        later_same_day = f.run(self.dir, f.Fetcher(FakeFTC(data)), NOW + dt.timedelta(hours=2))
        self.assertEqual(later_same_day["requests"], 0)
        next_day = f.run(self.dir, f.Fetcher(FakeFTC(data)), NOW + dt.timedelta(hours=24))
        # The newly published day is the first of the FRESH_DAYS being refreshed,
        # and the day that just aged past them is left alone.
        self.assertEqual(len(next_day["fetched_days"]), f.FRESH_DAYS)
        self.assertEqual(next_day["fetched_days"][0], "2026-09-24")

    def test_refuses_to_publish_a_list_that_collapsed(self):
        f.run(self.dir, f.Fetcher(FakeFTC(complaint_stream(days=10))), NOW)
        # Simulate an FTC outage: every stored day comes back empty.
        for path in (self.dir / f.DAYS_DIR).glob("*.json"):
            record = json.loads(path.read_text())
            record.update(counts={}, complaints=0)
            path.write_text(json.dumps(record))
        with self.assertRaises(SystemExit):
            f.publish(self.dir, [], [TODAY], 0, NOW)
        self.assertEqual(len((self.dir / f.LIST_FILE).read_text().split()), 3)  # untouched

    def test_a_day_too_big_for_one_run_is_finished_over_several(self):
        data = complaint_stream(days=1, per_day=1_000)  # 20 full pages, then an empty one
        fake = FakeFTC(data)
        yesterday = TODAY - dt.timedelta(days=1)
        for _ in range(3):
            summary = f.run(self.dir, f.Fetcher(fake, budget=8), NOW)
            self.assertTrue(summary["changed"])  # progress is published even mid-day
        # 8 + 8 + 5 requests: every page of the day fetched exactly once.
        day_requests = [u for u in fake.urls if yesterday.isoformat() in u]
        self.assertEqual(len(day_requests), 21)
        self.assertEqual(f.load_day(self.dir, yesterday)["complaints"],
                         sum(1 for _, _, p in data if f.normalize(p)))
        self.assertFalse(f.partial_path(self.dir, yesterday).exists())

    def test_stale_progress_is_dropped(self):
        data = complaint_stream(days=1, per_day=1_000)
        f.run(self.dir, f.Fetcher(FakeFTC(data), budget=8), NOW)
        yesterday = TODAY - dt.timedelta(days=1)
        self.assertEqual(f.load_partial(self.dir, yesterday, NOW)["offset"], 400)
        a_day_later = NOW + dt.timedelta(hours=f.REFETCH_AFTER_HOURS + 1)
        self.assertEqual(f.load_partial(self.dir, yesterday, a_day_later)["offset"], 0)

    def test_old_days_age_out(self):
        stale = TODAY - dt.timedelta(days=f.WINDOW_DAYS + 30)
        f.save_day(self.dir, stale, {"9092456175": 9}, NOW)
        f.save_partial(self.dir, stale, {"offset": 50, "found": {}}, NOW)
        f.run(self.dir, f.Fetcher(FakeFTC([])), NOW)
        self.assertFalse(f.day_path(self.dir, stale).exists())
        self.assertFalse(f.partial_path(self.dir, stale).exists())

    def test_output_is_ascending_ten_digit_lines(self):
        f.run(self.dir, f.Fetcher(FakeFTC(complaint_stream(days=10, per_day=300, seed=7))), NOW)
        lines = (self.dir / f.LIST_FILE).read_text().splitlines()
        self.assertEqual(lines, sorted(set(lines)))
        self.assertTrue(all(len(l) == 10 and l.isdigit() for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
