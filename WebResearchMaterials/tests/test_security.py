from app.services.security import hash_password, verify_password


def test_hash_and_verify_password():
    hashed = hash_password("mat-khau-an-toan")
    assert hashed != "mat-khau-an-toan"
    assert verify_password("mat-khau-an-toan", hashed)
    assert not verify_password("sai-mat-khau", hashed)


def test_password_must_have_eight_characters():
    try:
        hash_password("ngan")
    except ValueError as exc:
        assert "8 ký tự" in str(exc)
    else:
        raise AssertionError("Mật khẩu ngắn phải bị từ chối")

