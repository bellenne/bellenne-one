from app.services.auth import AuthManager


def test_password_hash_and_signed_session_are_safe(settings):
    auth = AuthManager(settings.data_dir)
    password = "correct horse battery staple"

    encoded = auth.hash_password(password)
    assert password not in encoded
    assert auth.verify_password(password, encoded) is True
    assert auth.verify_password("wrong password", encoded) is False

    session = auth.issue_session(42)
    assert auth.verify_session(session) == 42
    assert auth.verify_session(f"{session}tampered") is None
    assert auth.verify_session("not-a-session") is None


def test_username_and_password_validation(settings):
    auth = AuthManager(settings.data_dir)

    assert auth.normalize_username("  OWNER.User ") == "owner.user"
    assert auth.username_is_valid("пользователь_1") is True
    assert auth.username_is_valid("bad login") is False
    assert auth.password_is_valid("12345678") is True
    assert auth.password_is_valid("short") is False
