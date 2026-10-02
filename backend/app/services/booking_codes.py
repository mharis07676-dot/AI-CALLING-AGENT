import secrets
import string


def generate_booking_code(prefix: str = "BK") -> str:
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{prefix}{suffix}"
