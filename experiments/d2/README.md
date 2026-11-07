# D2 offline registry experiment

This is an isolated synthetic prototype, not an authority boundary or production-ready registry. It generates deterministic entries, refuses a source that enables an explicitly deferred class, detects byte drift, and denies unknown and disabled names.

The supplied rows and deferred-class list are untrusted synthetic inputs, not the canonical B2/B4 decision manifest. A malicious caller can omit a deferred class or supply a different registry. `verify_generated` must compare against independently authenticated canonical authority, and runtime lookup must be tied to that verified snapshot. This code does not establish either property. No C1 integration, class-to-capability completeness, authority update/revocation path, or runtime enforcement exists. T7, T36, B2/B4, C1, T22 and T39 acceptance remain open.

Run: `python3 -m unittest discover -s tests -v`.
