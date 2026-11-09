"""
github_git — Backwards-compatible alias for plugin/integrations/github.py.
"""

from pathlib import Path
import sys

_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

import github  # type: ignore

INTEGRATION_ID = "github_git"
DESCRIPTION = github.DESCRIPTION
provide_candidates = github.provide_candidates
provide_field_options = github.provide_field_options
