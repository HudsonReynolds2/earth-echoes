"""The export archive (task E4.8; spec 8.3, 13; phase-4 fixed choice 4; D178).

**A plain `.zip`** (fixed choice 4, addendum SPEC-13-01). Every Listener secret
inside is already firmware ciphertext. Zip rather than tar.gz because cards are
written on Windows field laptops, which open a zip natively.

The zip itself is deterministic for fixed members: sorted entries, a fixed
timestamp, fixed permissions. The MEMBERS are not, because each export draws
fresh key material (E4.5). That is why the archive is stored rather than
re-rendered on download (D178).
"""

import io
import zipfile
from collections.abc import Mapping

#: The zip format cannot represent dates before 1980.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

#: `rw-r--r--` regular files. The plaintext Aggregator login is protected by
#: who may download (MANAGE_PROVISIONING) and by the archive being encrypted at
#: rest, not by permission bits that do not survive a FAT-formatted card.
FILE_MODE = 0o100644


def build_zip(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(members):
            info = zipfile.ZipInfo(path, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE << 16
            archive.writestr(info, members[path])
    return buffer.getvalue()


def read_zip(archive: bytes) -> dict[str, bytes]:
    """Members by path. Used by tests and by any check that wants to verify
    what a download contains."""
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        return {name: opened.read(name) for name in opened.namelist()}
