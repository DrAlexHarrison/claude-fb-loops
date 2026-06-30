"""fb-assist — privacy-preserving feedback co-authoring toolbox (Build 3).

See ../01-build3-fb-assist-spec.md for the full design. The first building block
is :mod:`fb_assist.transcripts` — the streaming session-transcript extraction
engine that locates any category of content (with precise uuid+field+char-span
locators) for the co-author and the redaction module to act on.
"""

from . import transcripts  # noqa: F401
from .redact import (  # noqa: F401
    CATEGORIES,
    Finding,
    anonymize_pii,
    apply_redactions,
    leak_scan,
    merge_redaction_spans,
    reversible_tokenize,
    scan_pii,
    scan_secrets,
    strip_categories,
    summarize_findings,
)

__all__ = [
    "transcripts",
    # redaction toolbox (the detection + redaction floor)
    "CATEGORIES",
    "Finding",
    "scan_secrets",
    "scan_pii",
    "anonymize_pii",
    "reversible_tokenize",
    "apply_redactions",
    "merge_redaction_spans",
    "strip_categories",
    "leak_scan",
    "summarize_findings",
]
