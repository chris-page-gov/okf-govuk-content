# Citation verification

Status: **BLOCKED**
Snapshot: `T0-20260712`

- Released claims: 128
- Citation links: 196
- Unique sources: 111
- Passed: 160
- Non-dependent waivers: 0
- Per-citation failures: 36
- Joint claim reviews: 40/44 passed
- Blocking failures: 40

## Verification boundary

Transport, redirect, identity marker, locator, excerpt, hash, coverage, and binding checks are deterministic. Semantic support is accepted only from a separately recorded manual review bound to the exact claim and fetched document hashes.

A URL/title/token match never sets semantic support. The release verifier
requires a separate manual locator review bound to both the claim hash and
the fetched document hash. Any changed claim or source invalidates that review.

## Blocking failures

- `CIT-0047B465E23C3A5B`: missing evidence observation; missing independent semantic-support review
- `CIT-0487204FB8E6D2CB`: missing evidence observation; missing independent semantic-support review
- `CIT-05FEB1C62D28DAC9`: missing evidence observation; missing independent semantic-support review
- `CIT-0698FA3095F26CC2`: missing evidence observation; missing independent semantic-support review
- `CIT-06BCCFDEF287056F`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-167F8936D19D538E`: missing evidence observation; missing independent semantic-support review
- `CIT-1C450602B86004FC`: missing evidence observation; missing independent semantic-support review
- `CIT-215429D84F677D75`: missing evidence observation; missing independent semantic-support review
- `CIT-23C933C409DFA0EA`: missing evidence observation; missing independent semantic-support review
- `CIT-272B8C74C864F9EF`: source_evidence: claim-specific evidence set does not exactly match source citations; missing or duplicate claim-specific locator evidence; missing independent semantic-support review
- `CIT-29A911F919385FB5`: missing evidence observation; missing independent semantic-support review
- `CIT-2C64CE66F8E013A9`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-2FD078AB0DAEB286`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-302DD00271D74B87`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-40BBBCB77256338B`: missing evidence observation; missing independent semantic-support review
- `CIT-4EBD941FD2CCDB33`: missing evidence observation; missing independent semantic-support review
- `CIT-522A67E00F77EB3A`: missing evidence observation; missing independent semantic-support review
- `CIT-59D95F7D400EA000`: missing evidence observation; missing independent semantic-support review
- `CIT-5BDABF8ADF24C524`: missing evidence observation; missing independent semantic-support review
- `CIT-69D82CD2724F8944`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-76516C53CA06A210`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-80D7C290F664A7BE`: missing evidence observation; missing independent semantic-support review
- `CIT-82CB309905EA54C3`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-8A79E16323E0AF39`: missing evidence observation; missing independent semantic-support review
- `CIT-93BAC2B010C0EE18`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-A4D425E8092F10D9`: source_evidence: claim-specific evidence set does not exactly match source citations; missing or duplicate claim-specific locator evidence; missing independent semantic-support review
- `CIT-A68EE6C8E8493B94`: missing evidence observation; missing independent semantic-support review
- `CIT-A8B900D25AEF003D`: source_evidence: claim-specific evidence set does not exactly match source citations; missing or duplicate claim-specific locator evidence; missing independent semantic-support review
- `CIT-ADFDC231020892EF`: missing evidence observation; missing independent semantic-support review
- `CIT-B14596FDC5A35FDC`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-C89D295331814C1D`: missing evidence observation; missing independent semantic-support review
- `CIT-CC30A7C7BE240739`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-DA2859BB0FDED4D7`: missing evidence observation; missing independent semantic-support review
- `CIT-E76C9E79DD5A9AAB`: missing evidence observation; missing independent semantic-support review
- `CIT-F059C705BB0D40D0`: source_evidence: claim-specific evidence set does not exactly match source citations
- `CIT-FA497B072CF7E505`: missing evidence observation; missing independent semantic-support review
- `None`: missing joint semantic-support review for multi-source claim
- `None`: missing joint semantic-support review for multi-source claim
- `None`: missing joint semantic-support review for multi-source claim
- `None`: missing joint semantic-support review for multi-source claim
