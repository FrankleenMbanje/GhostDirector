# The Fame Files — Strategy Notes (2026-09-26)

**Mission:** YouTube Partner Program by 2026-10-26 (30 days). Daily automated production
(08:00 Africa/Harare): one trending SHORT first, then the 8-min+ long-form on the same
story. Everything uploads UNLISTED — Frank reviews and publishes.

---

## 1. Channel snapshot (audited 2026-09-26 via API)

| Video | Type | Category | Views | Likes | Notes |
|---|---|---|---|---|---|
| Travis Kelce Likely Owes Wife Taylor Swift… | Short 0:44 | 22 P&B | **1,605** | 33 | Best performer |
| Taylor Swift Just Broke Hollywood… | Short 0:55 | 24 Ent | **1,558** | 25 | |
| Tom Cruise Is the Last Great Movie Star… | Short 0:54 | 22 P&B | **1,433** | 15 | |
| Taylor Swift's Secret Zero Code… | Short 0:50 | 24 Ent | 343 | 0 | Freshest short |
| The Suspicious Death of Hayden Panettiere… | Long 8:20 | 24 Ent | 49 | 4 | 34.6% retention |
| The $9 Billion Lie: Elizabeth Holmes | Long 3:30 | 24 Ent | 15 | 2 | |
| How Tupac's Murder Was FINALLY Solved… | Long 7:42 | 24 Ent | 12 | 1 | 34.5% retention |
| Showgirl trademark doc (9:49) | Long | 24 Ent | 1 | 0 | Unlisted, awaiting review |

Subs: **5**. All videos now have the **AI-generated content box ticked** (containsSyntheticMedia,
retro-applied + automatic on every future upload).

## 2. What's working / what's not

**Working**
- **Shorts on ACTIVE trending celebrities.** 343–1,605 views per short on a 5-sub channel —
  the Shorts feed is doing the discovery. Taylor Swift content is 3 of the top 4.
- **The trending-news machine.** Fresh story → same-day short = the only repeatable view source.
- **Long-form retention is stable (~34–35%)** — the audience that clicks stays a third of the
  video. That's a floor to build on, not a broken product.

**Not working**
- **Long-form discovery: 12–49 views.** A 5-sub channel gets no browse/search traction for
  docs, no matter how good. Long-form is currently paying the rent for nobody.
- **Old/cold topics as long-form** (Tupac murder 30y, Panettiere death) — zero momentum.
- **Like rate is tiny (≤2%)** — shorts get watched but the audience isn't bonding (see §5).
- **Sep 23: the SAME Tom Cruise story was produced 4 times** — fixed now (no-repeat picker,
  ledger + DB checked before every auto-pick).

## 3. Category: "People & Blogs" (22) vs "Entertainment" (24)

The view spread in our data is a **short-vs-long split, not a category split** — both categories
appear on both sides. Category barely affects distribution. What the data does say:

- **Standardize on Entertainment (24)** for everything celebrity-news: it's where YouTube's
  celebrity audience sits, it matches the niche the templates were built for, and mixed
  categories muddy the channel identity signal. (P&B is for vlogs/personal brands.)
- **What actually gets views** (our data, and it matches how Shorts work): a vertical video
  about a celebrity with active same-week news momentum. Trending momentum > category > title.
- Templates now emit `category: 24` everywhere (verify `templates/*.json` in next pass).

## 4. The 30-day monetization math (honest version)

YPP thresholds: **1,000 subs + 4,000 public watch-hours** (long-form), OR **1,000 subs +
10M public Shorts views in 90 days**.

- Shorts views do **not** count toward the 4,000 hours. Long-form watch-time is the only
  classic path. At our measured ~170 s average view per doc, 4,000 h ≈ **85,000 doc views**.
  We're at ~77 doc views total. **The gap is ~1,000x — 30 days is not realistic on long-form alone.**
- The Shorts path (10M in 90 days) needs ~111k views/day. Our best day ever is ~1,600.
- **Realistic read:** monetization lands when a short (or three) goes properly viral and the
  funnel converts that audience into doc watch-hours + subs. Our job in 30 days is to
  maximize ticket count in the viral lottery while keeping the funnel intact:
  1. **Daily cadence doubles our lottery tickets** (short + doc every day, 8:00, automated).
  2. **Shorts stay short** (40–55 s, hook < 12 words — the gate enforces this).
  3. **The doc rides the same story the short proved** — a short that pops pushes feed
     viewers to the channel, where the same-day doc harvests watch-hours.
  4. **Publish same-day.** Unlisted videos earn nothing. Review each morning, flip public.
  5. **Evergreen backup:** each doc is searchable for months (the 4,000 h clock is 12 months
     rolling — docs published now keep earning the threshold).

## 5. Why older viewers (the "WHY")

The audience is 45+ and that's not an accident — **every layer of the product codes as
"TV documentary / tabloid news"**:

1. **Topics:** Tom Cruise, Tupac, legacy rise-and-fall moguls — nostalgia figures. Trending
   news search over-samples legacy stars because that's what news desks cover.
2. **Voice:** authoritative male narrator (Guy/Andrew neural), measured documentary pacing.
3. **Look:** the EXCLUSIVE news-split thumbnail = Inside Edition / tabloid TV grammar.
4. **Feed logic:** Shorts distributes to viewers who already watch this grammar — older
   cohorts who watch celebrity docs on TV.

**Should we "fix" it? Mostly no — monetize it:**
- Older viewers **finish long-form** (the stable 34% retention) → watch-hours path.
- They're ad-tolerant and watch on TVs/laptops → longer sessions.
- Trade-off: they like/comment rarely (our ≤2%) and subscribe less impulsively — so subs
  grow slower; expect it, don't panic.
- **Targeted youth tweaks worth testing (later, not now):** 1–2 shorts/week on Gen-Z
  celebrities/influencer drama, faster cut pacing on those only.

## 6. The daily workflow (built today, FIX-067)

- **08:00 Africa/Harare daily** — Windows Task "GhostDirector Daily Production"
  (StartWhenAvailable catches up after sleep/reboot; 8 h execution limit).
- Stage 1: **short** (trending template) → Stage 2: **8-min+ doc** (celebrity_8min template)
  on the same fresh story. Short failure never blocks the doc.
- **No repeats:** every auto-picked story is checked against the full ledger + DB first;
  repeats only if literally every candidate is old.
- **8-min floor:** long-form target is a hard floor (min_ratio 1.0) + expansion passes.
- **AI disclosure box:** set automatically on every upload (`containsSyntheticMedia`).
- **Privacy re-assert guard:** after upload, the uploader polls processing and re-asserts
  `unlisted` on every poll (drift guard, 2026-09-26 incident) and logs a wedged transcode
  (the 33 h `processing` bug) instead of shipping blind.
- **Runbook if processing wedges >1 h again:** byte-identical re-upload; verify fresh copy
  reaches `processed` BEFORE deleting the stuck one (proven live: 33 h wedged vs 60 s fresh).

**Ops requirements (Frank):** PC on + internet up at 08:00; publish the previous day's
videos each morning; re-do the OAuth consent if a token dies (7-day testing-mode expiry —
consider moving the Google OAuth app to production status); weekly: run `--sync-analytics`
and read §2 numbers again.

## 7. 30-day scoreboard to watch (next update: 2026-10-03)

1. Shorts: median views per short (baseline ~1,000; want a 10k+ hit in the month)
2. Docs: views + avg view duration (baseline 170 s; want 25%+ of short viewers clicking a doc)
3. Subs: 5 → ? (want 100+; shorts end-screen + pinned comment funnel)
4. Zero repeat topics (ledger check), zero un-reviewed-public videos, zero wedged uploads
5. Watch-hours banked toward 4,000 (rolling 12 mo — docs keep accruing after the sprint)
