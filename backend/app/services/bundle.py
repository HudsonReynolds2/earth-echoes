"""E5.10: the stack bundle as bytes, byte-identical every time.

The platform stores no archive (fixed choice 7). `GET .../stack/download`
re-renders from the committed rows and streams the result, and the property
that makes that legitimate is that **two downloads are byte-identical** — an
operator who downloads twice must not get two different bundles whose
credentials both claim to be current.

Rendering being pure is necessary and not sufficient: `tar` and `gzip` both
embed timestamps and ownership by default, so an archive of identical files is
still different on every call. Every one of those is pinned below. This is the
whole reason the archive is built here rather than with a `tarfile` one-liner
at the call site.
"""

import gzip
import io
import tarfile
from collections.abc import Mapping

from app.services import stack, stackgen

#: The directory every path in the archive sits under, so unpacking cannot
#: scatter files into the operator's current directory.
ROOT = "echoes-stack"

#: Pinned so the archive is reproducible. Real epoch seconds would make every
#: download differ; 0 is the only value that is stable AND obviously synthetic
#: rather than a lie about when the bundle was made.
EPOCH = 0

#: Config files are readable by the compose services; private keys and `.env`
#: are not. The broker container reads its own key as uid 1883, so the key is
#: 0644 by necessity and the archive's own warning is what protects it (the
#: same trade `devbroker.write_artifacts` documents).
MODE_FILE = 0o644
MODE_SECRET = 0o600

#: Files whose contents are credentials in directly usable form. Kept 0600 so
#: an unpacked bundle is at least not world-readable on a shared host.
SECRET_PATHS = frozenset({".env", "mosquitto/server.key", "prometheus/scrape_password"})


def bundle_files(
    generated: stackgen.GeneratedStack,
    tls: Mapping[str, str],
) -> dict[str, str]:
    """Every file in the bundle, as path → text.

    `tls` comes from `stackgen.tls_material`: generated ONCE at POST and
    stored, never regenerated here. A fresh certificate per download would be a
    different bundle every time and would break every Aggregator already
    trusting the old CA.
    """
    files = dict(stack.render_configs(generated.spec))
    files["mosquitto/ca.crt"] = tls["ca.crt"]
    files["mosquitto/server.crt"] = tls["server.crt"]
    files["mosquitto/server.key"] = tls["server.key"]
    files["README.md"] = stack.readme(generated.spec)
    files[".env"] = "".join(f"{key}={value}\n" for key, value in sorted(generated.env.items()))
    return files


def build_archive(files: Mapping[str, str]) -> bytes:
    """A deterministic `.tar.gz` of the bundle.

    Four sources of nondeterminism, all pinned: entry order (sorted), entry
    mtime (`EPOCH`), owner uid/gid and names (0 and empty — a real uid would
    leak the API container's user into every operator's archive), and gzip's
    own header timestamp (`mtime=0`, which `tarfile`'s `w:gz` mode does NOT
    let you set, hence the explicit `GzipFile`).
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            payload = files[path].encode("utf-8")
            info = tarfile.TarInfo(name=f"{ROOT}/{path}")
            info.size = len(payload)
            info.mtime = EPOCH
            info.mode = MODE_SECRET if path in SECRET_PATHS else MODE_FILE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(payload))

    compressed = io.BytesIO()
    # `mtime=0` is the point: gzip stamps the current time into its header by
    # default, which alone would make two downloads of identical bytes differ.
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=EPOCH) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


def unpack(archive: bytes, destination: object) -> None:
    """Unpack a bundle to a directory. Used by the tests and the rig; the
    platform itself never writes one to disk."""
    from pathlib import Path

    target = Path(str(destination))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(target, filter="data")
