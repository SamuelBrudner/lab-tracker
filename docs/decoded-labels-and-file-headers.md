# Decoded Labels and File Headers

Two capture paths read machine-readable data a capture already carries and
record it as note metadata:

- **Codes in photos** (server): when a photo is uploaded, QR codes and 1D/2D
  barcodes in it are decoded into `decoded_session_link_code`,
  `photo_session_id`, `barcode_gs1_*`, and `barcode_text` metadata.
- **Instrument file headers** (`lt watch`): FCS, OME-TIFF, and NWB files
  carry `format_kind`, `format_acquired_at`, and other `format_*` metadata
  read from their headers.

## This is decoding, not OCR

OCR-based transcription stays deferred (see the Restoration Ledger in
[retained-v1-surface.md](retained-v1-surface.md)). Nothing here recognizes
printed or handwritten text. A QR code, a DataMatrix, a Code 128 barcode, and
an FCS or TIFF header are formats designed to be read by machines, and they
are read here with fixed rules: the same bytes decode to the same metadata
whenever they are decoded at all. No model or provider is called. Decoding runs on the Lab Tracker
instance (photos) or on the watching machine (file headers), and nothing is
sent anywhere else. The results are ordinary note metadata. When a person
later asks for a graph draft, that metadata goes into the note's review
packet like any other metadata.

Decoding is never required and never blocks a capture. A photo with no code,
an unreadable image, a missing optional package, or an exceeded limit
produces the same capture without the decoded keys, and a malformed file
header adds only `format_kind` and `format_sniff_error`.
The one thing the metadata leads to is a *proposed* provenance link (see
[Session labels](#session-labels)), which a person accepts or rejects in
review.

## Codes in photos

### Setup

Install the optional `decode` extra on the server, for example with
`uv sync --extra decode` in a checkout or `pip install "lab-tracker[decode]"`.
It adds `zxing-cpp` and Pillow. Wheels exist for CPython 3.10 to 3.12 and
later on Linux (x86-64 and ARM64), macOS, and Windows. Without the extra,
uploads behave exactly as before.

When the extra is installed, decoding is on by default: it is local and
deterministic, and it only adds metadata.

- `LAB_TRACKER_DECODE_PHOTO_CODES=false` turns decoding off.
- `LAB_TRACKER_DECODE_PHOTO_CODES_TIMEOUT_SECONDS` (default `1.5`) is the
  longest an upload waits for its decode.

See [configuration.md](configuration.md#uploads-and-managed-files).

### Effort per capture

None. Take the photo with the label in the frame: a session's capture QR, a
tube or rack label printed with the session's `LT-<code>`, or the GS1
DataMatrix on a reagent box.

### Which uploads are decoded

Decoding runs on `POST /notes/upload-file` and `POST /notes/quick-capture`
when the upload is a JPEG, PNG, WebP, GIF, or BMP image. That covers:

- phone and web photos;
- the photo half of a photo+voice bundle (each part of a bundle is its own
  upload sharing a `capture_bundle_id`; the photo note gets the decoded keys,
  the voice note does not);
- share-target captures;
- offline-queued photos, when they upload;
- image files synced by `lt watch`.

TIFF (usually microscopy stacks), HEIC, SVG, and PDF files are not decoded.
Only the image decoder for the declared content type may open the bytes, and
the file must really be in that format. For example, an EPS or PDF labelled
`image/png` is stored as uploaded but never decoded. None of the five
decoders starts a subprocess.

### What is recorded

| Key | Value |
| --- | --- |
| `decoded_session_link_code` | `LT-<code>` of a session named in the photo |
| `decoded_session_link_code_count` | distinct sessions named in the photo |
| `photo_session_id` | that session's id, only when it is the one session in the note's own project |
| `barcode_gs1_gtin` | GS1 AI 01, 14 digits, check digit verified |
| `barcode_gs1_lot` | GS1 AI 10 (up to 20 characters) |
| `barcode_gs1_expiry` | GS1 AI 17 as an ISO date (`YYYY-MM-DD`) |
| `barcode_gs1_serial` | GS1 AI 21 (up to 20 characters) |
| `barcode_gs1_catalog` | GS1 AI 240 (up to 30 characters) |
| `barcode_text` | the first other code's text, bounded to 256 characters |
| `barcode_text_format` | that code's symbology, e.g. `QRCode`, `Code128` |
| `barcode_count` | distinct codes decoded (at most 16 are considered) |

The server owns these keys. A request that creates a note with one of them
(`POST /notes`, `/notes/upload-file`, `/notes/quick-capture`, or an evidence
bundle's source note) is refused with `422`. `PATCH /notes/{id}` replaces the
whole metadata bag, so it may send a decoded key back unchanged or drop it (a
person correcting a misread). Adding a decoded key or changing its value is
refused. When a capture is replayed with the same `client_capture_id`, the
replay is matched against the original with these keys ignored, because the
decode is best effort.

### Session labels

A decoded code names a session in one of two forms:

- an `LT-<code>` token, which must be printed exactly as the app shows it.
  This is the same rule the watcher uses for folder names: the `LT-` prefix
  is required and the 26-character code must be canonical, so arbitrary
  letters never claim a session.
- the session's **Capture into this session** QR, which is a
  `/app/capture?...&session_id=<uuid>` link.

`photo_session_id` is set only when exactly one decoded session belongs to
the note's own project. If the code names a session in another project, or
two different sessions in the project appear in the same photo, the note
keeps `decoded_session_link_code` but no `photo_session_id`. Nothing is
revealed about sessions outside the project.

`photo_session_id` is one of the exact-id detector's session keys, along
with `watch_session_id` and `capture_session_id`. At the next batch run, the
detector proposes a `was_derived_from` link from the note to that session,
with `basis: exact_id_match`, on the provenance review surface. The session
never becomes a note target by itself. A note that already has the session
as a target proposes nothing, and a declined pair is not proposed again.

### GS1 labels

GS1-128, GS1 DataMatrix, GS1 QR, and GS1 DataBar symbols carry an element
string: Application Identifiers (AIs), each followed by its data. The parser
in `lab_tracker/gs1.py` handles:

- AIs of 2, 3, or 4 digits, where the first two digits fix the length.
- Predefined-length AIs (`00`-`04`, `11`-`20`, `31`-`36`, `41`), which take
  no separator.
- Variable-length AIs, which end at an FNC1 separator (ASCII GS, `<GS>`, or
  `␝`) or at the end of the data.
- An optional symbology identifier such as `]C1` or `]d2`.
- The human-readable `(01)...(17)...(10)...` form, even inside a plain QR or
  Code 128.

For expiry dates, `DD=00` means the last day of the month, and the two-digit
year follows the GS1 century rule. Bare digits in a symbol that is not
flagged as GS1 are not treated as GS1. A malformed element string (an unknown
AI, short fixed-length data, a bad GTIN check digit, an over-long lot or
serial, or an impossible date) falls back to `barcode_text`.

### Limits and upload latency

Decoding runs inside the upload request, before the note is written.
Because of that, the decoded keys are already in the upload response, and no
second write can race a reviewer's edit. (A background task would need its
own claim-and-merge protocol, as automatic transcription has, and the phone
would never see the result.) The added time is bounded:

- A typical 12 MP phone JPEG decodes in about 0.1 to 0.4 s on one core,
  which is less than the upload itself takes on a phone network.
- Uploads over 32 MiB are not read for decoding.
- An image whose header declares more than 50 MP is skipped before any
  pixels are decoded. For formats other than JPEG, which have to be decoded
  at full size, the limit is 24 MP.
- Decoding works on a grayscale copy at most 4096 px on its long side. A
  much larger JPEG is decoded directly at reduced scale.
- The upload waits at most the configured time budget (default 1.5 s). After
  that it proceeds without decoded metadata, and the abandoned decode
  finishes in the background within the limits above.
- At most two photos are decoded at once per server process. A photo that
  arrives while both slots are busy is not decoded, rather than queued.

A timeout, a busy skip, a corrupt image, or a decoder error is logged as a
warning or at info level. The upload still succeeds.

## Instrument file headers (`lt watch`)

### Setup

No setup is needed beyond `lt watch`: every file a watch observes is
sniffed. NWB fields need `h5py` in the client environment. Without it, an
`.nwb` file is still labelled `format_kind=nwb`, with
`format_sniff_error="h5py not installed"`. Set
`LAB_TRACKER_WATCH_FORMAT_SNIFF=0` to turn sniffing off
([configuration.md](configuration.md#lt-client-and-agent-setup)).

### What is read

Formats are recognized by their magic bytes, not their file names.

| Format | Read from | Keys |
| --- | --- | --- |
| FCS 2.0/3.0/3.1/3.2 | HEADER TEXT offsets, then the TEXT segment (delimiter escaping handled) | `format_version`, `format_date` (`$DATE`), `format_begin_time` (`$BTIM`), `format_end_time` (`$ETIM`), `format_instrument` (`$CYT`), `format_original_filename` (`$FIL`), `format_event_count` (`$TOT`), `format_parameter_count` (`$PAR`), `format_source` (`$SRC`), `format_operator` (`$OP`) |
| OME-TIFF (classic or BigTIFF) | First IFD's ImageDescription (OME-XML) | `format_version` (schema), `format_image_name`, `format_size_x/y/z/c/t`, `format_pixel_type`, `format_instrument` (first microscope), `format_objective`, `format_objective_magnification` |
| NWB (HDF5, with `h5py`) | Root `session_start_time`, `identifier`, `session_description` (datasets, else attributes); `/general/subject/subject_id` | `format_version`, `format_session_start_time`, `format_identifier`, `format_session_description`, `format_subject_id` |

`format_kind` is `fcs`, `ome_tiff`, or `nwb`. A plain TIFF without OME-XML
gets no `format_*` keys. These keys go into the event's source and from
there into the synced note's metadata, next to `watch_*`. With the
`acquisition-output` sink they stay on the outbox event.

### Acquisition time

`format_acquired_at` is ISO-8601 UTC. It comes from:

- FCS: `$DATE` plus `$BTIM`, or `$BEGINDATETIME` in FCS 3.2.
- OME: the first image's `AcquisitionDate`.
- NWB: `session_start_time`.

`format_acquired_at_timezone` says how the timezone was decided:

- `header`: the header clock carried a UTC offset, so the conversion is
  exact.
- `local:+HH:MM`: the header clock had no offset (FCS `$DATE`/`$BTIM`, or a
  naive OME `AcquisitionDate`). It was read as local time on the machine
  running `lt watch`, which is usually the acquisition workstation or shares
  its timezone, and the offset used is recorded.

Sub-second FCS fractions are dropped. A clock that cannot be parsed leaves
`format_acquired_at` out and keeps the raw header fields. Time-window session
proposals prefer `format_acquired_at` over a note's observed or creation time
when it is present.

### Limits and safety

- No more than 2 MiB is read from any file: the FCS TEXT segment up to
  1 MiB, the first TIFF IFD up to 4096 entries, and up to 1 MiB of
  ImageDescription. OME-XML parsing stops at the first image's `Pixels`
  element, so large plane lists are never read.
- NWB values are read only from the file itself: h5py follows hard links
  only (never soft or external links), and a value is read only when it is a
  scalar string of at most 4 KiB stored in the file (not HDF5 external raw
  storage or a virtual dataset). Variable-length strings are read by the
  sniffer, within the 2 MiB budget, because HDF5 would first allocate
  whatever length the file claims. HDF5's own parsing of the object headers
  and messages it walks is not counted in that budget. h5py opens the file
  read-only with HDF5 file locking off (h5py 3.5 or later), so a scan never
  makes acquisition software that is writing the file fail with "unable to
  lock file".
- Every value is limited to 256 characters.
- OME-XML with any DTD or entity declaration is refused, so external entities
  (XXE) and entity expansion ("billion laughs") cannot happen. `defusedxml`
  is used when it is installed; otherwise the standard-library parser is
  used, with the DTD refusal applied first.
- A malformed or over-limit header records `format_kind` and a bounded
  `format_sniff_error` and never fails the scan. An unrecognized file gets no
  keys. `h5py` is imported only when an NWB candidate is sniffed.
