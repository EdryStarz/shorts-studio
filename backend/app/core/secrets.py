import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required for connected accounts")
    return Fernet(key.encode("ascii"))


def encrypt_credentials(credentials: dict) -> str:
    return _fernet().encrypt(json.dumps(credentials).encode("utf-8")).decode("ascii")


def decrypt_credentials(value: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(value.encode("ascii")))
    except (InvalidToken, json.JSONDecodeError) as exc:
        raise RuntimeError("unable to decrypt platform credentials") from exc

