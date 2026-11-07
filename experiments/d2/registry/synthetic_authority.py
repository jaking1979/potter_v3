"""SYNTHETIC test trust configuration; never a canonical production authority.

These values deliberately live outside the caller-supplied manifest. Replacing
this file is a trust-config/code change requiring independent review, not a
runtime flag. An authenticated Notion/B2/B4 source is still absent.
"""
EXPECTED_SCHEMA = "potter.d2.synthetic-manifest.v1"
EXPECTED_VERSION = "fixture-1"
REQUIRED_CLASSES = ("read", "voice", "host-write")
DEFERRED_CLASSES = ("voice", "host-write")
EXPECTED_DIGEST = "2cd882e086ae33948078b951ed22f9c3efe80b7263f9c39cac02cd29915e986d"
