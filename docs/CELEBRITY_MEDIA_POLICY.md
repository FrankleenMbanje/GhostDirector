# Celebrity photos and real footage

The edit must show the actual named subject using verified reusable media.
Generic buildings, random stock people, AI celebrity likenesses, and unrelated
stock video are not substitutes for missing celebrity media.

Cropping, mirroring, muting, speeding up, adding captions, or limiting a clip to
six seconds does not establish permission or guarantee fair use. An official
website, search result, lack of a watermark, or archive upload is not itself a
reuse license. The earlier local samples were not verified reusable media packs
and must not be published as such.

The local `licensed_subjects` path checks each Commons file's specific license
and source page. Its initial allowlist is CC BY, CC0, and explicitly documented
public-domain files. It rejects unknown, noncommercial, no-derivatives and
share-alike licenses; share-alike needs a separate implementation of its terms.
Public-domain evidence retains the original file page and its stated scope;
US-government public-domain status is not a blanket statement about all rights
or every jurisdiction. File titles/descriptions filter subject candidates;
they do not replace inspecting the actual faces and footage.

Every selected asset is recorded in `media_rights.json` with its creator,
license URL, evidence, source page, selected-file SHA-256 and modifications.
`MEDIA_CREDITS.txt` contains the attribution text. Packaging retains this text,
and compliance rejects missing credits or changed/unregistered selected files,
including paired cutaways. The unverified YouTube hook step is bypassed.
The package requires both real subject footage and photographs. If those cannot
be sourced, production stops rather than inventing a completed celebrity edit.

## Replacement sample prepared for acquisition

Use the White House's real Stevie Wonder footage, with original narration about
his 2014 Medal of Freedom, and separately verified subject photographs. Keep the
celebrity visible, use restrained shot changes, and fit captions away from faces.
Mute any source music and use separately licensed background music. Inspect each
chosen excerpt for third-party material before calling the sample ready.

- [Verified footage page and file-specific public-domain evidence](https://commons.wikimedia.org/wiki/File:Presidential_Medal_of_Freedom_Recipient_-_Stevie_Wonder.webm)
- [Official facts and ceremony date](https://obamawhitehouse.archives.gov/the-press-office/2014/11/24/remarks-president-presentation-medal-freedom)
- [Candidate subject photo and creator evidence](https://commons.wikimedia.org/wiki/File:Stevie_Wonder_performs.jpg)
- [Commons reuse requirements](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)
- [YouTube's fair-use guidance](https://support.google.com/youtube/answer/9783148)

## Validation and current limits

Thirteen offline tests pass for license selection, subject matching, source hosts,
attribution retention, selected-file integrity, paired unverified footage, and
missing manifests. Python compilation passes. A local speech synthesis with
Edge TTS 7.3.1 succeeded; requirements now include the released client fix.

Live Commons searches returned documented reusable Stevie Wonder footage and
photos, plus CC BY Taylor Swift photos. The API required an identifying user
agent containing the repository URL. License URLs with and without trailing
slashes now normalize identically; license versions must match. Subject names
must appear in the file title, and crowd/tribute/record-label graphics are
excluded rather than treating a mention in a description as subject evidence.

After the approval service became available, the specifically reviewed Stevie
Wonder footage and photo downloaded successfully. The 23.13-second local sample
at `output/sample-celebrity-v3/sample_v3.mp4` contains seven subject shots with
archive-date labels, original narration, and separately credited music. Its
`sample_manifest.json` and `MEDIA_CREDITS.txt` preserve the sources and changes.
The full export decodes successfully. It has not been uploaded.

GitHub regression CI and deployment of this follow-up are still pending. The
earlier production repairs remain separate from these local media-policy edits.
