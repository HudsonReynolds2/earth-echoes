"""The provisioning tool (epic E4; spec 8, and 16.4's bootstrap block).

Generates the files a field tech writes to microSD cards and tracks which
card belongs to which device. **This package writes files, never cards**
(spec 8.1): the browser does not touch hardware.

The vocabularies live in `app.models` beside the CHECK constraints they
render, so the two cannot disagree; they are re-exported here for callers
that think in provisioning terms rather than table terms.
"""

from app.models import BUNDLE_MODES, BUNDLE_SCOPES, RECORD_FILE_KINDS, RECORD_STATUSES

__all__ = ["BUNDLE_MODES", "BUNDLE_SCOPES", "RECORD_FILE_KINDS", "RECORD_STATUSES"]
