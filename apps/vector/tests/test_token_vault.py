from app.services.token_vault import TokenVault


def test_token_vault_encrypts_and_persists_key(tmp_path):
    vault = TokenVault(tmp_path)
    encrypted = vault.encrypt("wb-secret-token-123456")

    assert "wb-secret" not in encrypted
    assert vault.decrypt(encrypted) == "wb-secret-token-123456"
    assert vault.hint("wb-secret-token-123456") == "wb-s••••3456"

    reloaded = TokenVault(tmp_path)
    assert reloaded.decrypt(encrypted) == "wb-secret-token-123456"

