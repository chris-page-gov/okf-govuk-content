# GOV.UK Chat comparison walkthroughs

These assets define replayable, non-personal comparison journeys between
GOV.UK Chat and the What’s on GOV.UK OKF bundle.

The comparator and bundle have different jobs. GOV.UK Chat synthesises an
answer from retrieved GOV.UK content. The OKF bundle is a metadata-led
discovery and provenance layer: it should identify canonical records, stable
source-native identities, route and relationship context, evidence, snapshot
and an authoritative GOV.UK hand-off. It must not pretend that retained
metadata is the current page body.

`new-parent-multi-service.json` is the first prompt and capture contract. It
combines the multi-service-life-event and parent/carer stories and uses only
non-personal hypothetical questions. Capture each answer with its ordered
GOV.UK source cards and UTC retrieval time. Never store the tester’s account
identity, tokens, cookies, conversation identifier or personal information.

`official-published-example.json` records one genuine question/answer example
from the official GDS launch screenshot. To keep the observation compact and
reproducible without republishing a long answer, it stores the question, one
short excerpt, a structured paraphrase, the screenshot digest and the two GOV.UK
source destinations shown in the answer. It is published evidence, not a live
replay of the five-turn walkthrough.

GOV.UK Chat observations are time-sensitive comparator evidence, not gold.
Verify claims against the linked current GOV.UK pages, then compare each source
URL with the bundle record, route state, stable ID, relationships and evidence
locator. If the hydrated snapshot does not contain a returned URL, record that
as an explained coverage gap; do not silently substitute a different record.

The public Chat surface is in the signed-in GOV.UK app. The production web
interface is a departmental testing surface protected by GOV.UK Signon. Do not
automate around that access boundary.
