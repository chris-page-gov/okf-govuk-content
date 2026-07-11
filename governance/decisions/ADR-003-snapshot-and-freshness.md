# ADR-003: T0/T1 snapshots require byte-stable enumerator sets

Status: accepted  
Date: 2026-07-11

Sitemap shards changed during the source audit without a coherent index
watermark. A census is accepted only when every sitemap/index/Search partition
response is retained with retrieval time, byte length and SHA-256 and a
verification pass confirms the same complete set. A change restarts only the
affected enumerator snapshot. T1 repeats the contracts and closes the delta.

The published freshness target is a dated machine release, not a claim of
instantaneous parity. Scheduled drift checks may be incremental, but a release
must publish its exact T0/T1 window and maximum source lag.

