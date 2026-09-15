# Catalog data quality

The catalog separates discovery from authority. A source can help find an activity without being
allowed to establish its canonical identity or critical dates.

## Source roles

1. An organizer's official event page is authoritative for the event name, venue, schedule and
   canonical URL.
2. A franchise or artist's official page is authoritative for its own participation. For a broader
   festival, it proves an affiliation rather than ownership of the whole event.
3. A ticket platform is authoritative for the rounds and deadlines it sells. It does not replace
   the organizer's event page.
4. Editorial aggregators and community databases are discovery and enrichment inputs. Their facts
   must be linked to an official or ticket source before they become verified catalog facts.

`catalog_activity_subjects` records why an activity is relevant to a subject: direct ownership,
performer, collaboration, cast participation or source scope. It also stores the participant,
scope, evidence link and verification state. Migrated historical relations remain `LEGACY` and
unverified until checked.

## Collection and review

Official sources should prefer stable detail URLs discovered from an official sitemap. The sitemap
`lastmod` value supports incremental collection, while a bounded backfill covers older updates.
The detail page remains the evidence; a sitemap timestamp is only its publication/update time.

Model extraction must cite a contiguous evidence block for the activity, every milestone and every
subject affiliation. It may only use known subject slugs and links supplied by the source. Model
candidates remain in review. Deterministic structured ticket data can remain verified after its
source-specific validators pass.

For an editorial or community source, the publisher's own page cannot become the canonical event
URL. The candidate may use a linked organizer page on another domain or a concrete ticket page; if
neither is present, the canonical URL remains empty until review. Human approval verifies the
activity, milestones and every explicit subject relation together.

An explicit overseas performance marker in the title, city or venue fails domain validation. Its
raw page remains searchable, but the activity cannot enter the Japan catalog even through a
structured source or later review approval.

## Audit and repair

Run the read-only report inside the product image:

```bash
python -m genchi_product.quality audit
```

The report currently flags published records without verified evidence, community URLs used as an
official URL, unverified subject relations, transport products modeled as event occurrences and
ticket-source/URL mismatches. Rules generate a repair queue; they do not delete records.

The Aniera Festa 2026 repair is an idempotent golden-case migration. Preview it first, then apply it
only after a verified database backup:

```bash
python -m genchi_product.quality repair-aniera
python -m genchi_product.quality repair-aniera --apply
```

It replaces the community URL with the organizer URL, records RAISE A SUILEN as the scoped and
verified BanG Dream performer relation, and supersedes shuttle transport mistakenly modeled as
festival occurrences. Superseded occurrences and their orphaned milestones remain in the database
for provenance but are hidden from public presentation.

The conservative historical link repair only considers published activities whose canonical URL is
still on `bandori.fans`. It requires an exact title match to a collected BanG Dream official event
page and requires every extracted official date to be covered by an existing active occurrence.
Known overseas locations in the official body or existing venue exclude the record from this repair
and place it in the separate overseas-location audit. The preview and apply commands are:

```bash
python -m genchi_product.quality repair-community-links
python -m genchi_product.quality repair-community-links --apply
```

This rule replaces the canonical link and adds verified official evidence. It leaves the detailed
candidate in review and does not infer performer or ownership relations.

## Session semantics and coverage (2026-09)

The Lawson search results are **discovery summaries**, not complete schedules. Native `pfKey`
identifiers cannot be compressed to distinct dates. The collector reads the public `form-data`
JSON from the detail page, follows every advertised reception, and retains each reception's
explicit applicable session keys. Only `scheduleCompleteness=native_detail` can enter automatic
publication; bounded, missing, conflicting or redirected details go to review. Collection metrics
include `detail_pages` and `complete_details`; the detail cursor rotates the bounded work.

Never infer hours from opaque session IDs. Missing clock times remain DATE. Period passes use
both explicit date boundaries, not the `99999999` sentinel. Check the label beside the clock:
`開演`, `入場開始` and `入店開始` mean different things. A vendor's admission clock cannot replace
an organizer-confirmed performance clock. Exhibitions use admission slots/periods, hotel room
products use check-in dates, and live viewings use screening times. Each generated node contains
its date/time and venue; the original activity title is retained separately.

Ticket deadlines can differ by session within one reception. Native reception and session scopes
are separate identities. Identical facts may share a milestone; a later scoped change splits the
changed session without updating every other session. Sold out or reception closed does not mean
canceled. Pia cancellation must be grounded in its status component (`statusEvidence`), never in
generic refund/cancellation instructions elsewhere on the page. Legacy cancellation claims without
that evidence enter REVIEW, including when replayed through the pipeline.

The model prompt has the same time semantics and session-coverage requirements. All model output
continues to require evidence and review; prompting alone is not an authorization to publish.

### Historical repair procedure

Live collection and snapshot capture run on the production worker, never the developer's machine.
Keep browser snapshots outside disposable containers, take a verified database backup and pause
normalization/notifier processes during import and repair. Use previews before applying:

```bash
# Worker: writes the versioned raw resource through PostgresSink.
python deploy/import-lawson-native-snapshots.py /path/to/remote-snapshots
python deploy/import-lawson-native-snapshots.py /path/to/remote-snapshots --apply
# Product: retains existing activity IDs and only supersedes covered coarse dates.
python -m genchi_product.schedule_quality native
python -m genchi_product.schedule_quality native --apply
python -m genchi_product.schedule_quality labels
python -m genchi_product.schedule_quality labels --apply
python -m genchi_product.schedule_quality pia-status
python -m genchi_product.schedule_quality pia-status --apply
```

Repairs preserve raw versions, original entities, evidence, external mappings and subscriptions;
superseded nodes are retained. Each substantive repair records a non-notifying `DATA_REPAIRED`
change. Native replay is idempotent per resource version; label and status repairs are idempotent
by state. Absence from today's ticket page is not proof of cancellation: historical dates lacking
replacement evidence stay in the review backlog. Audit again after applying, verify public activity
responses and repeat previews to confirm no further mutations are proposed.

Regression coverage: `tests/test_lawson_detail.py` covers session/round identity, per-session sales
deadlines, admission periods, clock meaning, missing evidence and footer cancellation text.
PostgreSQL tests in `tests/test_product.py` cover scoped fact splitting and idempotent historical
repairs preserving IDs and suppressing notification changes.

After the native replay, run `deploy/finalize-schedule-replay.py` inside the product image with the
applied native report, first as a preview and then with `--apply`. It verifies committed repair
receipts against the current raw version, refreshes raw search and acknowledges the corresponding
jobs. Unresolved resources get persistent review dispositions. This avoids processing the same
historical batch again as newly discovered information when normalization resumes. A DATE ticket
explicitly present in native details is retained even when that date also has timed admission slots.
