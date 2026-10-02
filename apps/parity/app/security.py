import os
from cryptography.fernet import Fernet, InvalidToken


def cipher(settings):
    key = settings.encryption_key
    if not key:
        path = settings.data_dir / "credentials.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(Fernet.generate_key())
        key = path.read_bytes()
    return Fernet(key)


def decrypt(settings, encrypted):
    try:
        return cipher(settings).decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError):
        raise ValueError("credential_unreadable") from None


def account_view(account):
    # Never serialize ciphertext or decrypted secrets, even for the owner.
    return {key: getattr(account, key) for key in (
        "id", "marketplace", "name", "client_id", "status", "capabilities",
        "last_checked_at", "last_sync_at", "last_successful_sync_at", "last_failed_sync_at", "last_error",
    )} | {"secret_masked": "••••••••" if account.secret else None, "configured": bool(account.secret)}
