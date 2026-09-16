# Phase A fixtures

`jcs_golden_vectors.json` freezes product golden bytes; it is not a substitute for a
vetted RFC 8785 implementation or the upstream conformance suite.

`canary_fixture_suite.json` records all media evidence currently present and the exact
missing fixture work. A blocked or partial entry must never be counted as a Gate A
canary. Local `tmp/` paths are audit breadcrumbs only; production fixtures require an
immutable object URI, generation, size and SHA-256 plus authoritative labels.
