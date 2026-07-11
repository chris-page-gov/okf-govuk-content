# AF/HF GOV.UK OKF — controlling-clause traceability

Status: execution baseline  
Prepared: 11 July 2026  
Controlling file SHA-256: `d9dcf21a6dfc3eb5497e377ba8c978c3006229d83efffaf951c8d798d4a908c0`  
Requirements register: `AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md`

This populated crosswalk makes Acceptance Gate 1 auditable. Line ranges are 1-indexed in the exact controlling file above. `text_sha256` is the SHA-256 of the raw UTF-8 bytes in the inclusive line range, including original line endings. User-prompt clauses use a SHA-256 of the exact normalised clause text shown in the request.

## Original brief

| Clause ID | Lines | Text fingerprint | Obligation | Requirement IDs |
|---|---:|---|---|---|
| BRIEF-001 | 1–5 | `18adf5e9e31cc5df69eee04e92e3edda42c9d0cd8510457fe9e1f989734c726e` | Project identity, date and proposed repository | REQ-002, REQ-040 |
| BRIEF-002 | 9–12 | `7d1d14c3969831abd87e972c536f3fd4264bbd61af87c1e6bb0d6912f1f51a61` | Independent bundle mapping GOV.UK structure, content/navigation, organisations, metadata, taxonomies and relationships | REQ-002, REQ-014–REQ-018, REQ-035 |
| BRIEF-003 | 14–24 | `34004a7057cf61b22ea9e0e72de26ef84f929567404ab13ae248f8eb07143f0a` | Human navigation; system understanding; agent retrieval/citation; source/presentation/search comparison; reusable layer | REQ-003–REQ-007, REQ-069–REQ-080 |
| BRIEF-004 | 28–40 | `7a23d01b6e43942712a08d9c86fd4147bd54c3f0c73f1dd8b5c35a1bf132d642` | Metadata-led scope and ten required metadata families | REQ-009–REQ-023, REQ-037–REQ-039 |
| BRIEF-005 | 44–49 | `695c635054f5eb8f9bb86b96d3a6489ed6ba43fb09b6e210662d0c75654dcedb` | Fresh official-source audit; contract, coverage, access and reuse verification before acquisition design | REQ-024–REQ-029, REQ-032–REQ-034 |
| BRIEF-006 | 51–53 | `efa25dc253f6a490a4b78b6c9d03c2e9bf32af448118d2b5a4c9584a804544d3` | No silent omission; machine-readable constraint/escalation ledger | REQ-030–REQ-031, REQ-093–REQ-095 |
| BRIEF-007 | 57–69 | `745f705516cbf68f8dd37bbd1c4f51664cd485a323c33738af99c3b889cd1ac2` | Example GOV.UK and Schema.org node types | REQ-035, REQ-037–REQ-039 |
| BRIEF-008 | 71–87 | `3be8f756485e02bd4272f29a4c8e44b477dba78b3cc6e99f19cd139603d3b95b` | Relationship vocabulary and evidence/provenance per relationship | REQ-018, REQ-035–REQ-036, REQ-085 |
| BRIEF-009 | 91–100 | `d22b464eb2cf7fe76fb405a6168185430d7953412506eeab10803a2a54903ad9` | Federated repository, formats, descriptors, shards, adjacency, registry and checks | REQ-040–REQ-048 |
| BRIEF-010 | 104–118 | `adb4e95570e53db79bc414fe3fa28f2457ed0a0b6eaa77814927bd8a77f6e572` | Source audit, crosswalk, fixture, Explorer evaluation, full corpus, publication and labelled enrichment phases | REQ-013, REQ-024–REQ-025, REQ-037–REQ-039, REQ-044–REQ-047, REQ-075, REQ-080 |
| BRIEF-011 | 122–127 | `dd36e89d52baed679f523786f4b6852969de41f6015deddd68e8f4ed7ae895a7` | Repository, first-corpus scope, initial questions/personas and authenticated-source decisions | REQ-009–REQ-013, REQ-032, REQ-049–REQ-068, REQ-086–REQ-093 |

## User research and implementation request

| Clause ID | Exact clause summary | Text fingerprint | Requirement IDs |
|---|---|---|---|
| PROMPT-001 | Test the Agent First, Human Friendly discovery-layer proposal | `ec744f9cb343594de19f849608e844589040899d1033e32428069fc363b59f05` | REQ-001, REQ-069–REQ-080 |
| PROMPT-002 | Comprehensive detailed personas and stories covering each/every GOV.UK use | `a7e0cc3962c3f5196e2da649a737d37ffa361b10f4814fe6131aeea59933b580` | REQ-049–REQ-058 |
| PROMPT-003 | Construct 100 questions for each to test OKF/YAML-LD/Explorer effectiveness and efficiency | `e59a1f9fdea5f1ea307e47a0d3803d040a6ac2db2fdcd9790bf100e7210a8d68` | REQ-059–REQ-068, REQ-069–REQ-073 |
| PROMPT-004 | Detail Explorer changes needed for Human UI of choice | `0cf80b72f6c91752ca190c009978af85b852f92093a84078509b896167f0e586` | REQ-069–REQ-077 |
| PROMPT-005 | Compare with similar work from any source | `1da7a5f1c79c0207e4ccefbec87c0cd23ec5e978a889a923913df4ec82dbc5a6` | REQ-078–REQ-079, REQ-081–REQ-085 |
| PROMPT-006 | Analyse aims/objectives and how far the proposal fulfils them | `f17203e62a15f6b1c5f553373865e2dd15b7a917b1b8bb36610dfa67f9dd81e4` | REQ-001, REQ-080, REQ-095 |
| PROMPT-007 | Fully detailed parallel multi-agent/model plan executable unattended | `9c7996a537a670a129c3c4c1b0bdc49e46fabf2cacd9ad1660b364b9fb524cfa` | REQ-086–REQ-093 |
| PROMPT-008 | Transparent record of human/agent design, implementation, testing and evaluation | `ed54937db41b11b12b0d5cc9214cb2f324410d5f357b298a4e4fe6721e077eeb` | REQ-069–REQ-080, REQ-091, REQ-094–REQ-095 |
| PROMPT-009 | Detailed URL bibliography, precise locators and verification of every citation/URL | `f5a2cf0b4a92c4944667b98bdd6cf93df12268dfa61431cba51f69a96658d3ac` | REQ-081–REQ-085 |

## Later scope decision

| Clause ID | Exact text | Text fingerprint | Decision and requirements |
|---|---|---|---|
| USER-001 | “The initial implementation should include the entire gov.uk contents” | `5041bb1b36a945eb8ff53cf118a3d128e0973c999196da73acd1890ab89871d9` | ADR-000; REQ-009–REQ-013, REQ-033–REQ-034. It resolves BRIEF-011's all-versus-fixture choice in favour of the complete bounded metadata corpus. |

## Gate procedure

The controller verifies the controlling file hash and each line-range fingerprint before importing this table. It then checks:

1. every controlling clause has at least one requirement;
2. every requirement has a controlling clause or an explicit derived-control rationale;
3. every later instruction that supersedes a brief choice is represented by an ADR;
4. no mapped requirement is missing from the canonical requirements file;
5. changes to the brief or prompt invalidate the affected fingerprints and block promotion until the crosswalk is reviewed.

Requirements for deterministic controls, security, reproducibility and statistical validity may map to a broader prompt clause as derived implementation controls; `requirements.yaml` must store that rationale rather than inventing a new user obligation.
