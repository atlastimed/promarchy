"""Sign-in using Proton's SRP protocol.

The password never leaves this machine: SRP proves knowledge of it without
sending it. The SRP math comes from Proton's own ``python-proton-core``
(packaged in Arch's official repos), and the server-supplied modulus is only
used after its OpenPGP signature checks out against Proton's pinned key.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass

from .api import AuthExpired, ProtonClient, ProtonError, Session, WrongCredentials
from .paths import cache_dir

TWO_FACTOR_TOTP = 1
TWO_FACTOR_FIDO2 = 2


class MissingDependency(RuntimeError):
    pass


@dataclass
class HumanVerification:
    """Answer to a 9001 challenge, replayed as headers on the retried call."""

    token_type: str
    token: str

    def headers(self) -> dict[str, str]:
        return {
            "x-pm-human-verification-token-type": self.token_type,
            "x-pm-human-verification-token": self.token,
        }


@dataclass
class PendingLogin:
    session: Session
    two_factor: int

    @property
    def needs_totp(self) -> bool:
        return bool(self.two_factor & TWO_FACTOR_TOTP)

    @property
    def needs_fido2_only(self) -> bool:
        return bool(self.two_factor & TWO_FACTOR_FIDO2) and not self.needs_totp


def _proton_srp():
    try:
        from proton.session.api import SRP_MODULUS_KEY, SRP_MODULUS_KEY_FINGERPRINT
        from proton.session.srp import User
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise MissingDependency(
            "python-proton-core is not installed. Install it with:\n  omarchy pkg add python-proton-core"
        ) from exc
    return User, SRP_MODULUS_KEY, SRP_MODULUS_KEY_FINGERPRINT


def verify_modulus(armored: str) -> bytes:
    """Return the SRP modulus only if Proton's modulus key signed it."""
    _, key, fingerprint = _proton_srp()
    try:
        import gnupg
    except ImportError as exc:  # pragma: no cover - python-gnupg is a proton-core dependency
        raise MissingDependency("python-gnupg is not installed") from exc

    # A private GnuPG home keeps Proton's key out of the user's own keyring.
    home = cache_dir() / "gnupg"
    home.mkdir(mode=0o700, exist_ok=True)
    gpg = gnupg.GPG(gnupghome=str(home))
    gpg.import_keys(key)
    verified = gpg.decrypt(armored)
    if not (verified.valid and (verified.fingerprint or "").lower() == fingerprint):
        raise ProtonError("Could not verify the signature on Proton's SRP modulus; refusing to sign in")
    return base64.b64decode(verified.data.strip())


def begin_login(
    client: ProtonClient,
    username: str,
    password: str,
    hv: HumanVerification | None = None,
) -> PendingLogin:
    """Run the SRP exchange. Raises WrongCredentials / HumanVerificationRequired."""
    User, _, _ = _proton_srp()
    info = client.call("POST", "/auth/v4/info", body={"Username": username}, auth=False)
    modulus = verify_modulus(str(info["Modulus"]))

    srp_user = User(password, modulus)
    challenge = srp_user.get_challenge()
    proof = srp_user.process_challenge(
        base64.b64decode(info["Salt"]),
        base64.b64decode(info["ServerEphemeral"]),
        int(info["Version"]),
    )
    if proof is None:
        raise ProtonError("Proton sent an invalid SRP challenge")

    body = {
        "Username": username,
        "ClientEphemeral": base64.b64encode(challenge).decode(),
        "ClientProof": base64.b64encode(proof).decode(),
        "SRPSession": info["SRPSession"],
    }
    resp = client.call("POST", "/auth/v4", body=body, auth=False, headers=hv.headers() if hv else None)

    # Mutual authentication: make sure we are talking to the real server.
    srp_user.verify_session(base64.b64decode(resp.get("ServerProof") or b""))
    if not srp_user.authenticated():
        raise ProtonError("Proton's server proof did not verify; refusing the session")

    session = Session(
        uid=str(resp["UID"]),
        access_token=str(resp["AccessToken"]),
        refresh_token=str(resp["RefreshToken"]),
        user_id=str(resp.get("UserID") or ""),
        scopes=[str(s) for s in resp.get("Scopes") or []],
        created_at=time.time(),
    )
    two_factor = resp.get("2FA") or {}
    return PendingLogin(session=session, two_factor=int(two_factor.get("Enabled") or 0))


def submit_totp(client: ProtonClient, code: str) -> bool:
    """True when accepted, False for a wrong code that may be retried.

    Raises WrongCredentials when Proton locks the attempt (code 8002) and the
    whole sign-in has to start over.
    """
    try:
        resp = client.call("POST", "/auth/v4/2fa", body={"TwoFactorCode": code.strip()}, _retry_auth=False)
    except WrongCredentials as exc:
        raise WrongCredentials("Too many wrong codes. Start the sign-in again.") from exc
    except AuthExpired:
        return False
    if client.session is not None:
        client.session.scopes = [str(s) for s in resp.get("Scopes") or client.session.scopes]
    return True


def request_verification_code(client: ProtonClient, method: str, destination: str) -> None:
    """Ask Proton to send a human-verification code by email or SMS."""
    if method == "email":
        body = {"Type": "email", "Destination": {"Address": destination}}
    elif method == "sms":
        body = {"Type": "sms", "Destination": {"Phone": destination}}
    else:
        raise ValueError(method)
    client.call("POST", "/core/v4/users/code", body=body, auth=False)


def primary_address(client: ProtonClient, username: str) -> str:
    """The account's main address, used to name it.

    Signing in with a full address needs no lookup. For a bare username we
    read the address list; Proton includes the account's private keys
    (encrypted with your password) in that response, and Promarchy reads only
    the address fields and keeps nothing else.
    """
    username = username.strip()
    if "@" in username:
        return username.lower()
    try:
        addresses = client.call("GET", "/core/v4/addresses").get("Addresses") or []
    except ProtonError:
        addresses = []
    enabled = [a for a in addresses if isinstance(a, dict) and a.get("Status") == 1]
    enabled.sort(key=lambda a: a.get("Order", 1 << 30))
    if enabled and enabled[0].get("Email"):
        return str(enabled[0]["Email"]).lower()
    return username.lower()


REVOKED = "revoked"  # Proton confirmed the session is ended
GONE = "gone"  # the session was already invalid on Proton's side
UNCONFIRMED = "unconfirmed"  # could not confirm; the session may still be live


def end_session(client: ProtonClient, reload: Callable[[], Session | None]) -> str:
    """End the client's session on Proton's side and say what is known.

    A rejected refresh token alone does not prove the session is dead: the
    daemon may have rotated it a moment ago. So before believing that, we
    re-read the stored session; if someone else rotated the tokens, we retry
    with the live ones. Only an unchanged, rejected session counts as GONE.
    """
    for _ in range(3):
        if client.session is None:
            return GONE
        tried = client.session.refresh_token
        try:
            client.call("DELETE", "/auth/v4")
            return REVOKED
        except AuthExpired:
            try:
                stored = reload()
            except Exception:
                return UNCONFIRMED
            if stored is None or stored.uid != client.session.uid:
                return GONE  # signed out or replaced elsewhere; ours is dead
            if stored.refresh_token in (tried, client.session.refresh_token):
                return GONE
            client.session = stored  # rotated by another process: try again
        except ProtonError:
            return UNCONFIRMED
    return UNCONFIRMED


def logout(client: ProtonClient) -> bool:
    """Best-effort revoke of a session no one else holds (a sign-in that is
    being abandoned, or one just displaced). True when Proton confirmed it or
    the session is already dead; for stored sessions use end_session()."""
    try:
        client.call("DELETE", "/auth/v4")
        return True
    except AuthExpired:
        return True
    except ProtonError:
        return False
