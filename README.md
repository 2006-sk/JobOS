# JobOS

A job-posting monitor that runs entirely on GitHub Actions. It polls company
career-page APIs directly, filters for new-grad and internship roles in software
and adjacent fields, and pushes to your phone within ~30 minutes of a posting
going live.

It runs on Actions rather than a laptop because a laptop scheduler only fires
while the machine is awake, which defeats a monitor whose entire value is
applying within hours.

---

## How it works

```
config/companies.yaml ──► adapters (5 ATSes) ─┐
                                              ├──► relevance.py ──► state.py ──► notify.py ──► phone
aggregator feeds (3) ─────────────────────────┘      (filter)      (dedupe)       (ntfy)
```

1. **Fetch** — 32 company boards across five ATSes, concurrently, 8 at a time.
   Plus three community feeds as a safety net (see [Coverage](#coverage)).
2. **Filter** — deterministic title classification. No LLM anywhere in the
   polling loop: a model call every 30 minutes would cost money and hallucinate
   listings, and the decision is a regex problem.
3. **Dedupe** — `data/seen.json` records every id ever notified. New ids only.
4. **Notify** — one push per run, not one per role.

**Cron is best-effort.** GitHub delays scheduled workflows under load, sometimes
by 10+ minutes. "Every 30 minutes" is a target, not a guarantee.

---

## Setup

### 1. Pick an ntfy topic

Notifications go through [ntfy.sh](https://ntfy.sh) — free, no account needed.

> **ntfy topics are public.** Anyone who learns your topic can read it *and post
> to it*. Pick something unguessable — not `shresth-jobs`. For example:
> `joboS-7f3a9c2e4b1d`. Since this repo is public, the topic must live only in
> Actions secrets, never in a committed file.

### 2. Add the secret

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository
secret**:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your unguessable topic, e.g. `joboS-7f3a9c2e4b1d` |

### 3. Install the app and subscribe

Install ntfy ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
tap **+**, and subscribe to the same topic string.

### 4. Prove delivery works

Actions → **poll** → **Run workflow** → tick **test_notification** → **Run
workflow**. A sample push should arrive within seconds. If it doesn't, the topic
in the secret and the topic in the app don't match.

### 5. Let it run

The first scheduled run auto-bootstraps: it records every currently-open role as
seen and sends **zero** notifications. Without this you would get ~9,000 pings on
run one. From then on you only hear about genuinely new postings.

---

## Commands

```bash
make install     # runtime + dev dependencies
make test        # unit tests, no network
make lint        # ruff + mypy
make smoke       # LIVE: hit every board, print a status table
make backtest    # replay 30 days of feed data, show ping volume/day
make dry-run     # full pipeline, print the notification instead of sending
make bootstrap   # record everything as seen, send nothing
make finder      # discover new companies worth watching
```

`make smoke` exits non-zero if any board fails — run it after editing the
watchlist.

---

## Coverage

**32 of the 50 seeded companies have a verified, directly-pollable board.**

The other 18 run proprietary career sites or private ATS instances with no
public job-board API:

> Amazon · Apple · ByteDance · Citadel · Google · Hudson River Trading ·
> JP Morgan Chase · Meta · Microsoft · Oracle · Qualcomm · Susquehanna ·
> Tesla · TikTok · Two Sigma · Garmin · Rippling · Texas Instruments

Their apply URLs carry no ATS token to mine, and direct token guesses return
404 (Greenhouse/Lever/Ashby) or 422 (Workday).

**They are still monitored.** All 18 post into the three community aggregator
feeds, which are polled every run. That is what the "safety net" in
`adapters/aggregators.py` is for — it is load-bearing, not a nicety.

Aggregator listings are filtered to watchlist companies. The feeds cover ~3,450
companies; passing all of them through measured ~22 relevant roles/day versus ~4
when restricted. The feeds exist to cover watchlist companies without a board,
not to surface the entire job market.

---

## Tuning the filter

Everything personal lives in `config/profile.yaml`. You should never need to edit
code to change what pings you.

| Want to… | Do this |
|---|---|
| Stop hardware/QA roles entirely | remove `stretch` from `notifications.digest_tiers` |
| Get hardware roles instantly | move `stretch` into `notifications.instant_tiers` |
| Re-include citizenship-only roles | set `sponsorship.needs_sponsorship: false` |
| Change location ranking | reorder `locations.preferred` |
| Cap notification length | `notifications.max_roles_per_notification` |

Location **never** filters a role out — it only affects ordering.
`levels` treats new-grad and internship as equal priority; nothing in the code
ranks one above the other.

### Adding a company by hand

```yaml
# config/companies.yaml
  - company: "Figma"
    tier: "2"
    ats: greenhouse        # greenhouse | lever | ashby | smartrecruiters | workday
    token: "figma"
    # Workday only:
    # site: "External"
    # host: "figma.wd1.myworkdayjobs.com"
```

Then `python -m joboS.smoke --company Figma` to confirm it resolves. To find a token, open
the company's careers page and look at an apply URL — the token is the path
segment after the ATS domain (`job-boards.greenhouse.io/<token>/jobs/123`).

The weekly **finder** does this automatically: it mines the aggregator feeds for
companies posting relevant roles, requires ≥2 listings agreeing on a token,
verifies each with a live call, and opens a PR. It never auto-merges.

---

## Design decisions worth knowing

**`seen.json` is committed; `listings.json` is not.** The seen store is small,
append-mostly, and losing it would re-notify every job — so it needs to survive
anything, which the Actions cache does not (caches evict). The full board
snapshot is ~15MB of churning JSON; committing it 48×/day would add gigabytes of
history, so it's a workflow artifact instead.

**Notify first, record second.** If a push fails, the ids stay unrecorded and the
roles ping again next run. The reverse order buries a job permanently after one
transient failure — a silent miss, which is the worst failure mode here.

**Never scrape HTML, never touch LinkedIn.** Every source is a public JSON API.
LinkedIn's ToS prohibits automated access.

**Filter quirks that came from real data** (all in `relevance.py`):
- `intern` is a substring of *International* — every pattern is `\b`-anchored,
  or "Senior Product Manager, International Fulfillment" reads as an internship.
- *Member of Technical Staff* is an IC title, not a senior one. A blanket `staff`
  exclusion would drop "Member of Technical Staff New Grad".
- *Rising Senior* means a student.

So an explicit early-career marker overrides soft seniority words, while
managerial titles (manager, director, VP) are unconditional drops.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| No notifications ever | `NTFY_TOPIC` secret missing or differs from the app's topic. Run the test notification. |
| Thousands of notifications | `seen.json` was deleted or emptied. Restore it from git history — do **not** let it run. |
| A board shows `404` in smoke | Token changed. Find the new one from an apply URL, or drop the company. |
| "run FAILED" push | Unhandled exception; the Actions log has the traceback. |
| Cron drifted | Expected. Actions schedules are best-effort. |

---

## Layout

```
config/profile.yaml       you: fields, levels, locations, sponsorship
config/companies.yaml     the watchlist (generated + verified)
joboS/adapters/           one module per ATS + the aggregator feeds
joboS/relevance.py        title -> level/field/channel
joboS/state.py            seen store, the anti-duplicate-ping layer
joboS/notify.py           ntfy + Discord/Pushover/SMTP backends
joboS/poll.py             the run: fetch -> filter -> notify -> record
joboS/finder.py           weekly watchlist discovery, opens a PR
joboS/smoke.py            live board health table
joboS/backtest.py         replay feed history, measure ping volume
```
