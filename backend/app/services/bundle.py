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

MODE_FILE = 0o644
MODE_SECRET = 0o600
#: The dynamic security plugin REWRITES this file as the platform mints each
#: device's client (E5.6), and it arrives from the host owned by whoever
#: unpacked the archive rather than by the broker's uid.
MODE_DYNSEC = 0o666

#: Files kept 0600, so an unpacked bundle is not readable by every account on
#: a shared host.
#:
#: **Only `.env` qualifies, and the reason is worth stating because the
#: intuitive list is wrong.** These files are bind-mounted into containers that
#: drop to unprivileged users — Mosquitto to uid 1883, Prometheus to nobody —
#: and a 0600 file owned by the host user is unreadable to them. Setting the
#: broker's private key to 0600 does not protect it; it stops the broker from
#: starting at all, with `Unable to load server key file` and an OpenSSL
#: permission error. The keystone test found exactly that, which is what the
#: keystone is for.
#:
#: So the credential files the CONTAINERS read stay 0644 and the archive's own
#: "treat this as a credential" section is what protects them — the same trade
#: `devbroker.write_artifacts` documents for the dev broker. `.env` is
#: different: it is read by the `docker compose` CLI as the operator, so 0600
#: costs nothing.
SECRET_PATHS = frozenset({".env"})

#: Read by a container that has dropped privileges, so it cannot be 0600.
CONTAINER_READ_PATHS = frozenset({"mosquitto/server.key", "prometheus/scrape_password"})


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
            if path in SECRET_PATHS:
                info.mode = MODE_SECRET
            elif path == "mosquitto/dynamic-security.json":
                info.mode = MODE_DYNSEC
            else:
                info.mode = MODE_FILE
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
