"""PII protection at rest.

- Phone numbers are stored as an HMAC-SHA256 hash (keyed, so it can't be brute-forced
  without the key, even though there are only ~4 billion Indian mobile numbers).
  The same input always gives the same hash, so events can be linked to leads with
  a plain equality check. We also keep the last 4 digits for phone_masked.
- Other PII fields (customer_name) are encrypted with Fernet (AES + HMAC). That's
  reversible for someone holding the key, but not searchable.

The master key comes from the AU_PII_KEY environment variable. If it's not set we look
in .env, and if there's no .env either we generate a key and save it there.
Two separate sub-keys are derived from the master key, one for hashing and one for
encryption, so the same key material is never used for two purposes.
"""
import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

ENV_VAR = "AU_PII_KEY"
ENV_FILE = Path(".env")


def _read_env_file():
    """Return the AU_PII_KEY value from .env, or None."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == ENV_VAR and value.strip():
            return value.strip()
    return None


def load_master_key():
    """Get the master key from the environment, then .env, otherwise create one in .env."""
    key = os.environ.get(ENV_VAR) or _read_env_file()
    if key:
        return key
    key = secrets.token_urlsafe(32)
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ENV_VAR}={key}\n")
    print(f"Generated a new PII key and saved it to {ENV_FILE.resolve()}")
    return key


class Pii:
    """Hash phones and encrypt/decrypt other PII using keys derived from one master key."""

    def __init__(self, master_key):
        master = master_key.encode("utf-8")
        self.hash_key = hmac.new(master, b"phone-hash", hashlib.sha256).digest()
        enc_key = hmac.new(master, b"field-encryption", hashlib.sha256).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(enc_key))
        # A short, non-reversible fingerprint of the key, stored in the DB so we can
        # refuse to run with a different key (which would silently break linking).
        self.fingerprint = hmac.new(master, b"fingerprint", hashlib.sha256).hexdigest()[:16]

    def hash_phone(self, canonical_phone):
        """'+919876543210' -> hex HMAC. Only ever call this with a normalised phone."""
        return hmac.new(self.hash_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, text):
        return self.fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token):
        return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
