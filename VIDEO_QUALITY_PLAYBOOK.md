# Video Quality Playbook — "out of this world" (2026-10-04)

Operator brief: **no good video has shipped yet** — traffic so far came from topics
riding the algorithm, not from craft. This document is the field research, the
concrete definition of "world-class" for this channel, and the sequenced plan to
get there. It is the reference the pipeline code and QC gates are measured against.

---

## 1. What the field says (research, not vibes)

### Romayroh — faceless-YouTube operator (500M+ views claimed, "Faceless YouTube HQ")
Transcript pulled from his 2026 guide (`output/_research/romayroh/`). Core claims:
- The job is **pattern mining**: *"You just need to identify title patterns and
  thumbnail patterns from successful YouTube channels in your niche."*
- **The voiceover is the tell.** "Fixing the voiceover is what separates a real
  channel from a lazy one." Robotic, flat TTS reads as template spam even when the
  visuals are fine.
- Script must be **edited until it sounds like a person talking**, not a list of
  facts. (Our scriptwriter already writes in a documentary voice — the gap is the
  read, not the text.)
- He monetises the boring parts: process, consistency, and packaging.

### VidRush — the closest commercial competitor (their pipeline = ours, worse parts named)
VidRush sells "an AI production team for long-form": research → script → voiceover →
footage → editing → thumbnails. Reviewers (Trustpilot 4.4/50, multiple YouTube
reviews, Reddit postmortems) consistently flag:
- **"stock footage that doesn't always match the prompt"** — relevance is their weakness;
- low resolution, short clip lengths, credit costs;
- "quality consistency, manual cleanup" still required.
We already own the machinery they sell. Beat them on the two things they lose on:
**relevance and resolution** — plus real celebrity footage, which stock can't supply.

### Watermark-free sourcing (free, commercial-clear)
| Source | What it is | Notes |
|---|---|---|
| Pexels | 4K stock video + photos | no watermark, no attribution, API (already wired) |
| Pixabay | video + photos | no watermark, API (already wired) |
| Coverr, Mixkit, Videezy, Vidsplay, ISO Republic | curated free b-roll | no watermark; manual/limited API |
| Wikimedia / Wikipedia | portraits, event photos | high-res official shots (already wired) |
| archive.org | public-domain newsreels (pre-1964) | zero Content-ID risk (already wired) |
| YouTube excerpts | real interviews/press footage | LAST: 3–6s, muted/transformed, credited, provenance recorded (FIX-085 machinery) |

### Swipe-file harvest (this run)
1,347 videos collected (`output/_research/swipe/swipe.json`) from 10 niche queries.
**Caveat found by doing it:** generic seeds ("star", "celebrity") return music
videos and nursery rhymes, not celebrity docs — the sample is noisy. Lesson: the
next harvest must be **competitor-channel-seeded** (pull each competitor channel's
popular tab), not keyword-seeded. That work is queued below.

---

## 2. What "world-class" means here (measurable acceptance criteria)

Every upload must satisfy all of these; the QC gate should be able to prove each one:

1. **First 3 seconds:** motion + a face + on-screen text. No static frame ever.
2. **Cut rhythm:** a shot change every 2–4s (shorts) / 3–6s (docs); punch-ins at
   sentence boundaries; no shot longer than ~7s.
3. **Subject presence:** if a line names a person, that person is on screen —
   a face for ≥60% of person-scenes (now measured and preferred: FIX-095).
4. **No dead air:** narration runs to the final frame; tail silence fails (FIX-094).
5. **Resolution floor:** every asset ≥1280×720-equivalent at 1080p delivery; no
   640×360 rips (FIX-095).
6. **Sound design:** whoosh on cuts, sub-impact on reveals, music ducked under
   voice, act-change in the bed; −14 LUFS master.
7. **Captions:** word-level, 1–3 highlighted words, never full-sentence blocks.
8. **Grade:** one cinematic grade across stills + stock + real footage; grain and
   vignette consistent; no jarring colour jumps at cuts.
9. **Packaging:** title = one concrete name + one unresolved question (mining
   below); thumbnail = one subject, one emotion, 3–4 words, high contrast.

---

## 3. Already true in the pipeline (don't rebuild)

FIX-085 real-footage hook intro · FIX-087 strict celebrity gate · FIX-088
thumbnail dead-zone rebuild · FIX-089 delivery audit · FIX-090 run-scoped state ·
FIX-091 winner-riding docs · FIX-092 proven-name boost · FIX-093 kinetic hook card ·
FIX-094 no silent tails · FIX-095 asset floors + subject-aware photo selection.

## 4. Build order (each step lands with tests + a QC proof)

- **P0.1 — Real-footage scene blend (the "2Pac mix").** For scenes that name a
  person, fetch a 3–6s *real* interview/press clip (the FIX-085 machinery, per
  scene) and alternate it with the best photo of that person; photos stay the
  base layer, footage is the accent. Acceptance: ≥40% of person-scenes in a doc
  carry real footage; every clip 3–6s, ≥720p, provenance recorded.
- **P0.2 — Relevance gate at delivery.** Extend the existing Gemini frame review
  into a hard check: a sampled frame that contradicts its narration on a person's
  name is CRITICAL and triggers a re-fetch of that scene before upload.
- **P1 — Voice decision.** ElevenLabs (or equivalent) for docs if the operator
  approves the spend; otherwise edge-tts with stronger prosody modulation. Either
  way add a "does this read as human?" check on the first 10 seconds.
- **P1 — Engagement gate.** Compute cuts/min, motion fraction per sample, person
  face-coverage, and shot-length histogram in `final_qc`; block on the failures
  above (all are already measurable from existing probes).
- **P1 — Packaging engine.** Competitor-seeded swipe harvest → maintained
  "pattern book" (title shapes, thumbnail compositions) → fed into the
  scriptwriter/metadata prompts; YouTube A/B thumbnails on every upload.
- **P2 — Cut-cadence enforcement** for docs (punch-cuts already exist; make the
  3–6s cap a hard rule), SFX hit map, per-act music bed.

## 5. Open operator decisions

- **Voice spend:** pay for a top-tier TTS (ElevenLabs) on docs, or stay free
  (edge-tts) and invest the effort in prosody scripting?
- **Footage posture:** how aggressive to be with YouTube interview excerpts
  (monetisation risk) vs public-domain + stock only (safer, less "real").
