# The Fame Files: Signal Contrast

The composition communicates one true idea before the viewer reads a title. A genuine celebrity face anchors the image, with a readable expression from the original photograph. The design never fabricates a reaction, costume, prison scene or encounter. Carefully chosen source material does more work than extra decoration.

Deep charcoal creates a consistent field around warm white typography. One restrained accent separates the key word from the supporting words. The craft lies in calibrating that contrast and protecting the face; a bright outline or a pile of icons cannot substitute for it.

Scale carries the hierarchy. Large, closely fitted words balance the photograph across an off-centre division. Three or four words are the ceiling. Margins and line spacing receive the same attention as the headline so the image feels considered rather than assembled from stickers.

Depth comes from the photograph and its natural light. A soft local gradient joins it to the text field without inventing a new face or hiding publisher marks. Every crop must preserve eyes and mouth. The result remains coherent at 120 pixels wide; inspection at that scale is part of the design.

Each story receives three concepts and a phone-sized preview. Their purpose is a real editorial choice, not an unmeasured click-through-rate promise. The title states the news; the thumbnail supplies the visual emphasis. The finest refinement usually removes a competing element and makes the remaining elements read more clearly.

## Researched skills and free implementation

- Existing `canvas-design`: composition, hierarchy, restrained typography and a second visual pass.
- `sergebulaev/youtube-skills`, `yt-thumbnail-brief`: one focal point, truthful expression, short complementary copy and mobile inspection. MIT source: https://github.com/sergebulaev/youtube-skills/tree/main/skills/yt-thumbnail-brief . Optional publishing integrations are not used.
- Official Remotion skills: reusable frame-driven motion graphics. No paid image generator is needed for maps, typography or factual cards.

`local_studio/thumbnail.py` uses Pillow and licensed photographs locally. It exports three PNG/JPEG options below 2MB plus 120px previews. No Canva, Publora, Adobe, stock subscription or image-generation API is required.
