"""Make ``fb_os`` and its sibling ``fb_assist`` importable when running the test
suite straight from a source checkout (no install required)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_REPO, "fb-assist")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Keep transformers off the TensorFlow path (mirrors fb_assist.redact) so importing
# the redaction floor never trips the Keras-3 lazy TF import.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
