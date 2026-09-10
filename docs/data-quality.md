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
