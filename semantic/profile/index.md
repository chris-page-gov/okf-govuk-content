# GOV.UK OKF semantic profile v1

Status: experimental implementation profile. The versioned YAML-LD descriptor
is `govuk-okf-profile-v1.yamlld`; its `@context` resolves to the checked-in
`../context/govuk-okf-v1.jsonld`, so validation never depends on fetching a
mutable remote context.

## Scope and authority

This profile describes metadata for the complete, snapshot-bounded union of
admitted public GOV.UK sources. It does not mirror complete page bodies and it
does not make this derived catalogue authoritative. Canonical GOV.UK URLs and
source evidence remain the destination for reading or citing government
content.

Records and assertions use four non-interchangeable authority classes:

- `source_native`: directly observed in an admitted official response or
  version-pinned official contract;
- `normalized`: a deterministic, reversible canonicalisation of source data;
- `inferred`: rule-derived, evidence-linked and independently reviewable;
- `model_derived`: model-assisted, with model activity, prompt/version, usage,
  cost, confidence and review status retained outside the assertion itself.

Normalization never upgrades a statement to source-native. Inferred and
model-derived outputs cannot overwrite source-native values; conflicts remain
parallel assertions with their own evidence and validity.

## Identity model

`ContentItem`, locale-specific `Document`, lifecycle `Edition`, address
`Route`, and rendered `Part` are different entities. A redirect does not merge
routes, and a tombstone does not disappear because a replacement exists.
`ContentType` is the GOV.UK document type; `SchemaFamily` is the pinned
Publishing API schema contract. Their counts and identifiers are reported
separately.

Navigation also stays source-native: `Taxon`, `Browse`, `Collection`, and
`Service` are not aliases. `Service` is used only when an official field or
contract supports a genuine public service. `Organisation` roles are expressed
through typed assertions so publishing, ownership, sponsorship and leadership
remain distinguishable. `Attachment` publishes metadata, rights state and the
authoritative URL, never a silent copy of the asset.

## Assertion provenance

Every relationship is represented by one stable `Assertion` node with exactly
one subject, predicate and object. It records the source-native predicate or
field, one or more `Evidence` nodes, retrieval time, snapshot, acquisition or
derivation activity, authority class, method and numeric confidence. Normalized
or inferred assertions additionally identify their source assertions through
`derivedFrom`; inferred/model-derived assertions also carry review status.

Compiled convenience edges may exist for Explorer traversal, but they are
reproducible from assertion nodes and must retain an assertion identifier.
RDF-star is not required.

An `Evidence` node records source URL, source system, precise locator, retrieval
time, response/content hash, media type, source authority, licence and rights
status. Evidence stores only the minimum lawful metadata or excerpt fingerprint
needed for verification, not a complete page body.

## Interoperability boundary

- PROV-O maps evidence, acquisition/derivation activities and invalidation.
- SKOS maps taxonomy concepts and broader/narrower relations.
- ORG maps organisations and source-supported organisational relationships.
- Schema.org maps public web pages, attachments and genuine government
  services only where their semantics fit.
- CPSV-AP maps genuine public services; it is not applied to general guidance.
- DCAT maps catalogues, distributions, data services, versions and checksums
  only where the underlying item actually has those semantics.

Crosswalk entries are alignments, not declarations that every GOV.UK record is
conformant to every external vocabulary.

## Validation and versioning

JSON Schema checks record-level syntax and conditional provenance. The Turtle
shape graph expresses equivalent graph cardinality and type constraints. A
release must also expand YAML-LD and JSON-LD with the pinned context, canonicalise
both RDF datasets with the pinned RDFC-1.0 implementation and compare canonical
digests. That semantic-equivalence gate is performed by the release toolchain,
not claimed by these stdlib structural tests alone.

Breaking changes require a new profile and namespace version. Additive terms
may be introduced in a compatible minor version only when old consumers can
safely ignore them. Source-native/inferred separation and assertion evidence
requirements cannot be relaxed in a compatible release.
