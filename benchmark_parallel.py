"""Benchmark: sequential vs parallel batch collection on real store pages.

Same task list, same code path, only ParallelConfig.enabled differs.
Reports wall time for both modes and whether extracted data is identical.
"""
import json
import sys
import time

from scrapling_client import (CollectConfig, ParallelConfig,
                              collect_batch_sync)


def load_tasks():
    sites = {s["name"]: s for s in
             json.load(open("sites.json", encoding="utf-8"))["sites"]}

    def sel(name):
        site = sites[name]
        return {"item": site["item"], "title": site["title"],
                "price_now": site["price_now"], "link": site["link"],
                "image": site.get("image", "img")}

    return [
        {"url": "https://www.compumarts.com/search?q=iphone",
         "selectors": sel("Compumarts")},
        {"url": "https://www.compumarts.com/search?q=iphone&page=2",
         "selectors": sel("Compumarts")},
        {"url": "https://dream2000.com/search?q=iphone",
         "selectors": sel("Dream 2000")},
        {"url": "https://kimostore.net/search?q=iphone",
         "selectors": sel("Kimo Store")},
        {"url": "https://miamicenters.com/?s=iphone&post_type=product",
         "selectors": sel("Miami Centers")},
        {"url": "https://www.dubaiphone.net/search-results?q=iphone",
         "selectors": sel("Dubai Phone")},
    ]


def fingerprint(result):
    """Normalized comparable snapshot: sorted (link, title, price) per URL.

    Link query strings are dropped: Shopify appends per-visit session tokens
    (_sid/_pos/_ss) that differ between runs for identical listings.
    """
    from urllib.parse import urlparse
    snap = {}
    for item in result["items"]:
        rows = sorted((urlparse(row["link"]).path, row["title"], row["price"])
                      for row in item["rows"])
        snap[item["url"]] = rows
    return snap


def main():
    tasks = load_tasks()
    config = CollectConfig(delay=0.5)
    runs = {}
    for mode, enabled in (("sequential", False), ("parallel", True)):
        parallel = ParallelConfig(enabled=enabled)
        start = time.perf_counter()
        result = collect_batch_sync(tasks, config=config, parallel=parallel,
                                    fetcher="static")
        elapsed = time.perf_counter() - start
        total = sum(len(item["rows"]) for item in result["items"])
        runs[mode] = (result, elapsed)
        print("%s: %.1fs, %d urls, %d products, %d errors"
              % (mode, elapsed, len(result["items"]), total,
                 len(result["errors"])))
        for err in result["errors"]:
            print("  ERROR %s: %s" % (err["url"], err["error"]))
        time.sleep(5)

    seq_fp = fingerprint(runs["sequential"][0])
    par_fp = fingerprint(runs["parallel"][0])
    identical = seq_fp == par_fp
    if not identical:
        for url in seq_fp:
            if seq_fp.get(url) != par_fp.get(url):
                print("DIFF on %s: seq=%d rows par=%d rows"
                      % (url, len(seq_fp.get(url, [])),
                         len(par_fp.get(url, []))))
    seq_t = runs["sequential"][1]
    par_t = runs["parallel"][1]
    print("identical data: %s" % identical)
    print("speedup: %.2fx (%.1fs -> %.1fs)" % (seq_t / par_t, seq_t, par_t))


if __name__ == "__main__":
    sys.exit(main())
