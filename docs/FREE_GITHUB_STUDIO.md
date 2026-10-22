# Free GitHub daily video system

The owner authorized cloud production and unlisted YouTube uploads on 2026-10-19. The new `fame-review.yml` workflow is separate from the retired production workflows. It runs at 12:00 UTC (14:00 South Africa) and can be dispatched manually. Set repository variable `FAME_CLOUD_DAILY=false` to pause daily production. GitHub can delay scheduled starts.

The new publisher is explicitly restricted to the GhostDirector GitHub repository and The Fame Files channel UCkxIY_tv62QYMO5MOsBQp9w. It requests unlisted visibility, omits any public release schedule, verifies the returned visibility and channel, and checks for a previous upload for today's production before making another video. Public publishing is not supported. The legacy uploader stays blocked by LOCAL_EDITING_ONLY. Local review production still does not upload.

The production gates require at least five minutes of actual narration, multiple independent genuine celebrity video originals, real licensed photographs, original motion graphics and three thumbnail options. No stock footage, watermark erasure, repeated interview loop or silent padding fallback is allowed. Failure to find enough usable sources produces a clear blocked report, not a filler upload. A successful full cloud run must be observed before daily reliability is claimed.

Standard public GitHub runners have free compute under current rules: https://docs.github.com/en/billing/concepts/product-billing/github-actions . No paid or larger runners are used. The hosted job has a six-hour limit and this workflow stops at 350 minutes. AI and rendering run on the runner CPU, so completion within that limit remains to be tested. See https://docs.github.com/en/actions/reference/limits .

Artifact storage is separate and shared with other account usage. The optional review download is limited to 400 MiB; storage is refused if this repository's artifacts would exceed 450 MiB. Account-wide free storage and the Actions stop-usage budget must also be checked. No billing settings are changed. Review downloads expire after one day and are not a permanent footage library or a native Resolve project. Public-repository artifacts are not confidential.

Only the dedicated GD_YOUTUBE_TOKEN secret is supplied to publisher steps. No environment file, session cookies, model weights or credentials are included in download packages. The first successful cloud video should be watched through its unlisted link before depending on unattended operation. The old cloud upload workflows remain disabled. Disable the local Windows daily task when cloud production takes over to avoid two producers.

## Approved photo-led fallback
When clean licensed celebrity clips are unavailable, the daily edit may use at least six distinct genuine licensed celebrity photos with animated layouts, source-grounded geographic locators and motion graphics. Available verified clips remain eligible. No stock footage, fabricated celebrity pictures or erased watermarks. Reports disclose actual moving-video seconds; photo animation is not counted as source footage. Map geometry is bundled public-domain Natural Earth data; place lookups use OpenStreetMap Nominatim with attribution and rate limiting.

The cloud workflow saves a sanitized research checkpoint before visual screening and rendering. The same job continues from its complete local evidence after rechecking citations; a public redacted artifact is insufficient for continuation. Source publication dates are kept separate from retrieval dates. Editorial curation consolidates repeated claims and corrects historical reporting before script planning. Text inference uses the downloaded Qwen2.5 7B Instruct model; image screening uses the explicit Qwen3-VL 4B Instruct model.


## Dated editorial review run

The optional `editorial_package` dispatch input accepts a committed file inside `editorial/` for the current South African date. Scheduled runs leave it empty and continue to discover stories automatically. This path is an explicit source-reviewed fallback, not evidence that automatic research has become reliable.

The package's articles are retrieved again on the runner. At least six articles, three publisher domains, fresh reporting from two domains, sixteen distinct supported facts, four cited articles and three cited domains remain required. Every supplied supporting excerpt must survive revalidation. Prepared narration must cover six substantive chapters and at least 800 words with valid fact IDs. Normal media licensing, identity, mark screening, diversity, measured five-minute duration, full decoding and unlisted publishing checks still run. Optional map requests must be written in the supported facts and still pass the normal public geocoding checks.

The 2026-10-22 Dolly Parton package contains 25 source-reviewed facts and 861 words. A live recheck found all ten articles across four publishers, and the revised complete Edge narration measured 374.304 seconds locally. This proves source retrieval and narration, not a completed cloud edit. The automatic attempt on that date stopped at research with 14, 11 and 8 accepted facts for its three candidate stories; daily reliability remains unverified.

Narration attempts now have a three-minute absolute deadline, so websocket keepalives cannot leave a chapter stalled indefinitely. Existing bounded retries and completed chapter caches remain in use. Archive search reads three complete metadata batches, retains full category screening, and groups named crops of the same photograph as one original.


## Automatic production previews

Dispatch with `preview_only=true` to test the complete automatic path without channel authentication or uploads. The publisher also rejects preview mode before reading credentials. Preview runs use a separate concurrency group so they can validate automatic research while an authorized reviewed production continues.

Automatic extraction cites source-owned excerpt IDs instead of regenerating quotations. Editorial review issues an explicit keep/duplicate/unsupported decision for every proposed fact, preserving dates and speaker attribution without reducing the list to a headline summary. Publication dates travel with evidence into narration prompts. Song lyrics are excluded. All factual count and independent-source requirements remain in place.

Before full research, a topic must have enough eligible archive candidates for either the photo-led or classic edit. Candidate counts are not visual approval: individual identity, mark and clarity screening still follows. Story context disambiguates people with the same name, and contextual occupation terms narrow archive searches. These changes require a successful real automatic preview before unattended reliability can be claimed.


## Clean hybrid visuals and complete narration

A hybrid edit can use at least four distinct licensed photos and two distinct genuine video originals, with at least six independent visual originals overall. It requires at least 30% measured genuine moving footage, retains source-share and graphic-share limits, and rejects overlapping reused source segments. The existing six-photo fallback still applies when there are too few usable clips; the classic footage-led rules remain unchanged. Archive clips begin after their initial five percent, which avoids opening title cards without erasing any marks.

The reviewed Dolly shortlist uses four clear portraits and two licensed video originals, with an optional additional archival portrait still subject to visual screening. Exact Commons page IDs restrict the shortlist without bypassing licensing, identity or mark checks. The manually inspected video frame sheets showed no station or stock marks. Marked and off-subject images from the first failed visual pass are excluded. The vision check now requires a directly photographed real subject as the primary/co-primary focus and an empty detected-brand list; old screening caches are invalidated. File titles must name the intended subject.

Every narration attempt must provide timing for the complete source text and audio covering its last timed word before the MP3 and timing cache are committed. A partial stream is retried, and incomplete legacy cache entries are ignored. This repairs the cloud failure that returned only nine word events for chapter four. Live numeric speech and all six revised narration chapters passed the complete-word check locally.
