"""Re-fetch every cited URL and confirm the quoted detail and the person's name are really there."""
from __future__ import annotations

from . import fetch, research, store


def check_row(row: dict) -> tuple[bool, list[str]]:
    problems = []
    if not row["source"].startswith("http"):
        problems.append("no source URL")
    if not (row["detail_quote"] and row["detail_url"].startswith("http")):
        problems.append("no cited detail (quote + URL)")
    if problems:
        return False, problems
    try:
        dp = fetch.get(row["detail_url"])
    except fetch.Blocked as e:
        return False, [str(e)]
    if not dp.ok:
        problems.append(f"detail URL not fetchable (HTTP {dp.status})")
    else:
        if fetch.normalize(row["detail_quote"]).lower() not in research.searchable(dp).lower():
            problems.append("quoted detail NOT found at detail URL")
    try:
        sp = fetch.get(row["source"]) if row["source"] != row["detail_url"] else dp
    except fetch.Blocked as e:
        return False, [str(e)]
    if not sp.ok:
        problems.append(f"source URL not fetchable (HTTP {sp.status})")
    else:
        last = row["name"].split()[-1].lower()
        if last not in research.searchable(sp).lower():
            problems.append("person's name NOT found at source URL")
    return not problems, problems


def run(ids: list[str] | None = None, log=print) -> None:
    rows = store.load()
    targets = [r for r in rows if (r["id"] in ids if ids else r["status"] == "new")]
    results = {}
    for r in targets:
        ok, probs = check_row(r)
        results[r["id"]] = ("ok " + store.now_iso()[:10]) if ok else ("FAIL: " + "; ".join(probs))
        log(f"  #{r['id']:>3} {r['name']} ({r['company']}): {'OK' if ok else 'FAIL - ' + '; '.join(probs)}")
    with store.transaction() as rows:
        for r in rows:
            if r["id"] in results:
                r["factcheck"] = results[r["id"]]
