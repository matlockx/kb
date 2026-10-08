# Agent instructions: filling this knowledge base

This directory is a knowledge base built with `kb`. Read the engine's README first; this file covers what goes
into `domain.yaml` and `sources.yaml`, because a wrong entry here ends up cited as evidence.

## Sources

- Take a source from the body that publishes the text itself: the legislature or regulator for a law, the
  standards body for a standard, the author organisation for guidance. Not from a mirror, an aggregator, a
  search-result snippet, a blog post about the text or a summary written by a model.
- Never write a URL from memory. Open it, or take it from a link on the publisher's own index page (`kb links
  SOURCE` lists the links of a fetched page), and confirm that the page is the document named in `title`. A URL
  you have not seen resolve is not added.
- A secondary source (a commentary, a trade article, a vendor guide) is allowed when it is declared as one:
  its publisher gets `official: false` in `domain.yaml`.
- One entry per document. `title` is the document's own title, `language` the language of the text at the URL.
  A translation names its original with `translation_of`; the original is the binding text.
- Pick `doc_type` from `domain.yaml`; do not invent types or tags. Binding status belongs to the doc type, not to
  the single source.

## Publishers

- Every `publisher` in `sources.yaml` appears, spelled identically, under `publishers` in `domain.yaml` with the
  domains its URLs are on. `kb sources --check` fails for a source whose publisher is missing or whose URL is on
  another domain; that failure is the defence against look-alike and typo-squatted sites. Fix the entry; never
  widen `domains` to make a URL pass that you cannot justify.
- List the narrowest domain that belongs to the publisher (`legislation.gov.uk`, not `gov.uk`; the publisher's
  own host, not a hosting or CDN domain it shares with others).
- `official: true` only for a body that issues the texts itself. When unsure, write `false`: the source is then
  treated as secondary until a person vets it.

## Trust and reviews

- `kb quality` reports per source what was ingested, how many statements still have their quote in their section
  and how many count as evidence. `kb review` lists the trust level of every source and why.
- Vetting or disputing a source is a person's decision, recorded under their name (`kb name`). Prepare the
  evidence for them (the publisher's site, the document's date and status, what differs from the registry entry)
  and show it; record a verdict with `kb review SOURCE --vet` or `--dispute --note WHY` only when the person asks
  you to.

## After every edit

Run `kb sources --check` and fix what it reports instead of loosening the file. Changing `prompts/extract.md`,
modalities or topics re-extracts every section on the next `kb extract`; say so before doing it on a large corpus.
Write British English.
