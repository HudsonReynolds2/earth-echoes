"""Credentials a service can only issue once it is running (task E5.10).

Everything else in this phase generates a credential before the thing that will
accept it exists — that is fixed choice 7, and it is what lets a bundle be
rendered from stored rows and downloaded twice byte-identically. **Grafana
cannot participate in that.** Service account token values are issued by
Grafana at runtime and shown exactly once; there is no endpoint that reads one
back and no way to install one you chose. The only credential a fresh Grafana
accepts up front is `GF_SECURITY_ADMIN_USER` / `_PASSWORD`.

So the platform generates an admin account, and the FIRST verification uses it
once as a bootstrap: create the `echoes-platform` service account with the
Admin role, have Grafana issue a token for it, store the token as the
deployment's Grafana credential. Every test and every contact-point
registration after that sends the scoped token. The admin password stays in
SecretStore, unused, until a rotation needs it again.

**This is the one place in the phase where verifying can create something on a
target system**, and it is deliberately not inside a tester. E5.4d's rule that
provisioning is never a side effect of a test still holds for the testers
themselves: `GrafanaTester.run` writes nothing, and an operator's own Grafana
onboarded with a service account token never reaches this module at all. What
reaches it is a deployment whose operator supplied an ADMIN ACCOUNT — either by
typing one, or by asking the platform to generate the whole stack — which is a
request to have the platform set the account up.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.models import DeploymentService
from app.secrets import SecretStore
from app.services import store
from app.services.clients.grafana import GrafanaAdminClient
from app.services.clients.httpbase import ServiceDialError
from app.services.schemas import secret_name

log = logging.getLogger(__name__)

#: The secret field the minted token is stored under. The SAME field an
#: operator's pasted token uses, on purpose: once the bootstrap has run, a
#: generated stack and a bring-your-own Grafana are indistinguishable to every
#: reader, and nothing downstream needs to know which produced the token.
#:
#:
#: The name deliberately does not begin with `TOKEN_`. `SECRET_PATTERNS` in
#: `test_repo_layout` reads an uppercase TOKEN/SECRET/PASSWORD constant
#: assigned a 20-character string as a committed credential, and it is right
#: to: a regex cannot tell a field NAME from a field VALUE. Blunting the
#: scanner so this one line could keep a shorter name would cost far more than
#: the longer name does.
SERVICE_ACCOUNT_FIELD = "service_account_token"

#: The admin account's own fields. `admin_username` is plain settings and lives
#: in `config`; the password is a secret and lives under a SecretStore name.
ADMIN_USERNAME_FIELD = "admin_username"
ADMIN_PASSWORD_FIELD = "admin_password"


def needs_grafana_bootstrap(row: DeploymentService | None) -> bool:
    """Whether this Grafana row has an admin account and no token yet.

    Deliberately NOT "the stored token does not work". A token Grafana has
    revoked, or one an operator pasted wrongly, is a failure the tester should
    report with its remedy — silently minting a replacement would hide the
    operator's mistake and quietly issue credentials on every failed test.
    """
    if row is None:
        return False
    if row.secret_names.get(SERVICE_ACCOUNT_FIELD):
        return False
    return bool(row.config.get(ADMIN_USERNAME_FIELD)) and bool(
        row.secret_names.get(ADMIN_PASSWORD_FIELD)
    )


async def ensure_grafana_service_account(
    db: Session,
    secret_store: SecretStore,
    deployment_id: uuid.UUID,
    *,
    force: bool = False,
) -> str | None:
    """Mint the platform's Grafana service account token, if one is needed.

    Returns the field's SecretStore name when a token was issued, None when
    there was nothing to do. Commits nothing: the caller owns the transaction,
    for the same reason every other writer in this package does.

    `force=True` is rotation's entry point — it re-mints even though a token is
    already stored, which is the whole point of rotating one.

    **Stores the token before returning it anywhere.** Grafana shows a token's
    value once; a caller that failed between the issue and the store would
    leave a live token on the deployment's Grafana that the platform cannot
    present and cannot revoke by name.
    """
    row = store.load_service(db, deployment_id, "grafana")
    if row is None:
        return None
    if not force and not needs_grafana_bootstrap(row):
        return None

    username = row.config.get(ADMIN_USERNAME_FIELD)
    password_name = row.secret_names.get(ADMIN_PASSWORD_FIELD)
    base_url = row.config.get("base_url")
    if not (username and password_name and base_url):
        return None
    try:
        password = secret_store.get(password_name)
    except Exception:  # noqa: BLE001 - a row naming a lost secret is a real state
        log.warning(
            "grafana admin password %s is named on the row but unreadable; "
            "no service account was minted",
            password_name,
        )
        return None

    client = GrafanaAdminClient(base_url=str(base_url), username=str(username), password=password)
    async with client.session() as session:
        account_id = await client.ensure_service_account(session)
        token = await client.issue_token(session, account_id)

    stored_name = secret_name(deployment_id, "grafana", SERVICE_ACCOUNT_FIELD)
    secret_store.put(stored_name, token)
    # Re-read rather than reusing `row`: `issue_token` awaits, and the row is
    # only written after the secret is safely in the store.
    current = store.load_service(db, deployment_id, "grafana")
    if current is not None:
        current.secret_names = {**current.secret_names, SERVICE_ACCOUNT_FIELD: stored_name}
    log.info(
        "minted a Grafana service account token for deployment %s (account id %s)",
        deployment_id,
        account_id,
    )
    return stored_name


async def ensure_service_credentials(
    db: Session,
    secret_store: SecretStore,
    deployment_id: uuid.UUID,
    *,
    force: bool = False,
) -> list[str]:
    """Every bootstrap a deployment's services need before they can be tested.

    One entry point so the test endpoint and E5.11's rotation call the same
    thing, and so a sixth service that needs a runtime-issued credential has an
    obvious place to be added rather than a second call site to be forgotten.
    `force=True` is rotation's: re-mint even though a token is stored, because
    replacing it is the point.

    **A bootstrap that fails does NOT raise**, and that is load-bearing for
    rotation rather than merely tidy. The commonest moment to rotate is when a
    stack is unreachable — the operator has not restarted it yet, or its
    credentials have already stopped working — so a Grafana that cannot be
    dialled is an ordinary outcome and not a server error. The tester behind it
    reports the resulting auth failure with its remedy, one service's problem
    never blocks the other four (E5.3's rule), and the failure is logged by
    service key and never with the credential.
    """
    minted: list[str] = []
    try:
        name = await ensure_grafana_service_account(db, secret_store, deployment_id, force=force)
    except ServiceDialError as error:
        log.warning("could not mint a Grafana service account token: %s", error.failure.detail)
    except Exception:  # noqa: BLE001 - a bootstrap must not become a 500
        log.exception("unexpected failure minting a Grafana service account token")
    else:
        if name is not None:
            minted.append(name)
    return minted
