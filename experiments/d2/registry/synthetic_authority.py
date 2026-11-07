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

# Exact T14 definition digest over name, description and input_schema.
# Synthetic fixture only: this is not canonical admission or runtime authority.
TOOL_DEFINITION_PINS = (("notes.read", "c31c7260025d6b239a321ccc018efe614f6c421a54cb37dc9578d30404451f5f"),)
