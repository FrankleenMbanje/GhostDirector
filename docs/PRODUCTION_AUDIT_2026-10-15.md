# Production and growth audit — 15 October 2026

## Verified channel position

Read from the Fame Files OAuth profile at 10:32 Africa/Johannesburg:

| Measurement | Observed value |
|---|---:|
| Subscribers | 74 |
| Net subscriber growth, last 28 days | +61 |
| Lifetime views (Data API) | 48,256 |
| Public catalogue | 56 videos |
| Shorts views, 90-day Analytics report | 45,386 |
| Shorts engaged views, 90 days | 23,717 |
| Shorts watch hours, 365-day Analytics report | 228.017 |
| Video-on-demand watch hours, 365 days | 12.967 |
| Valid public watch hours / valid public Shorts views | Check Studio > Earn |

Analytics has reporting lag and includes watch time that does not qualify for
YPP. Neither lifetime views nor combined Shorts/documentary hours are eligibility
totals. The old multi-year ETA against a rolling 90-day threshold was invalid.

The [October 14 production run](https://github.com/FrankleenMbanje/GhostDirector/actions/runs/38576021057)
used commit `e46eeee4cfb7b8835636ade7fdaeda564196fce9`. Its green workflow result
represented a PARTIAL day: 2/3 Shorts and one documentary. Two Short attempts
failed at 66.0 and 60.67 seconds; the documentary was 450.19 seconds. The cloud
catalogue snapshot had 40 Shorts (median 1,065.5 views) and 16 documentaries
(median 36.5 views). Production volume has not translated into long-form reach.

## Defects repaired

1. Parallel scene preparation discarded `broll_video_path`. Paired footage was
   fetched but never reached the cutaway renderer. Preserve it in worker jobs.
2. Once cutaways were restored, mixed native footage/photo timebases corrupted
   concat timing. A real proof lost almost ten seconds of narration. Normalize
   video cuts to the photo pipeline's 30fps / 1:15360 track timebase; verify
   prepared-picture duration before muxing audio. QC now rejects exports ending
   before the narration timeline.
3. Sentence balancing interleaved nonconsecutive sentences and yielded bad cut
   boundaries. Use chronological groups; cap picture holds at four seconds for
   vertical video and six for landscape, including missing-timestamp cases.
4. A word estimate did not accommodate slow fallback TTS. Fit complete measured
   voice beats before sourcing assets, preserving the hook and ending. Never
   cut speech mid-sentence. Narration caches now require a matching text/voice
   fingerprint and invalidate stale word timings when narration changes.
5. Kinetic-card text and ASS hook text were burned together. Use a single hook
   headline layer. Evaluate actual picture shots rather than narration-scene
   count when scoring pacing. Keep the already-present local hook/runtime/QC
   delivery gates and thumbnail improvements in the release candidate.
6. Analytics sync erased imported CTR, parsed watch URLs incorrectly, flattened
   replay peaks, and could mutate packaging state during a dry run. Correct
   these and retain fractional watch minutes.
7. Growth reports/audits ignored channel selection and gave obsolete runtime
   advice. Add dated channel snapshots, 90/365-day content-type reports, net
   subscribers, and explicit unknown YPP eligibility. Preserve existing full
   YouTube consent, which was verified to read this channel's Analytics.
8. Carry the proven Short ID through re-syndicated winner headlines; scope
   companion lookup/authentication to the selected channel. Queue the clickable
   Studio Related video setup. Shorts comment URLs alone are not a funnel.
9. Integrate the existing local per-video Actions jobs, preserve analytics and
   packaging state between jobs/runs, retain timelines/scripts for audit, pin
   Ubuntu 24.04, and read the shared workflow start from the Actions API rather
   than the nonexistent `github.run_started_at` expression.

## Validation

The seven phase suites passed 659 checks. Production-health regressions include
a real 23.976fps-footage/still concat, voice-cache invalidation, measured Short
budgeting, worker cutaway preservation, channel growth windows, CTR preservation,
dry-run behavior, replay peaks, and eligibility honesty.

The cached September 30 Blake Lively Short was rendered into separate review
folders without TTS, research, uploads, or paid API calls. The restored-cutaway
proof initially ended at 36.83s against a 46.59s narration timeline; the new QC
guard rejects it. With normalized cut timing, the proof lasts 46.60s, contains
17 picture shots (maximum hold 3.749s), and passes deterministic final QC.
This verifies editing mechanics using older local media; it is not a fact-check
or publishing approval for that older story.

## Growth work and limits

Use Shorts for discovery and subscriber conversion; build a documentary around
a verified winning story, then set it as the Short's Related video in Studio.
Keep story relevance, a concise opening, and a complete payoff ahead of runtime
padding. Compare like-for-like content after it has had time to earn views;
the data does not justify promising a monetization date.

The cloud competitor report still showed business channels (MagnatesMedia and
Coffeezilla), which are not Fame Files' celebrity-news peers. Competitor inputs
need a separately verified niche watchlist before their outliers guide this
channel. Browser Studio access initially belonged to a different channel;
the owner is signing into The Fame Files for Earn totals and Related video work.

Sources: [YPP eligibility](https://support.google.com/youtube/answer/72851),
[Related videos](https://support.google.com/youtube/answer/14075157),
[Shorts link behavior](https://support.google.com/youtube/answer/13748639),
[Analytics reports](https://developers.google.com/youtube/analytics/channel_reports),
[retention metrics](https://developers.google.com/youtube/analytics/metrics).
