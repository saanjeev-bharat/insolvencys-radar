import os
from html import escape
from urllib.parse import quote

import httpx


def send_password_reset_email(to_email: str, token: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    frontend_url = os.environ.get("FRONTEND_URL")
    from_email = os.environ.get(
        "RESEND_FROM_EMAIL",
        "Insolvencys Radar <onboarding@resend.dev>"
    )

    if not api_key or not frontend_url:
        raise RuntimeError("Resend email configuration is incomplete.")

    separator = "&" if "?" in frontend_url else "?"
    reset_url = (
        frontend_url.rstrip("/")
        + separator
        + "reset_token="
        + quote(token, safe="")
    )

    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to_email],
            "subject": "Reset your Insolvencys Radar password",
            "html": (
                "<p>We received a request to reset your password.</p>"
                f'<p><a href="{escape(reset_url, quote=True)}">'
                "Reset your password</a></p>"
                "<p>This link expires in 30 minutes. "
                "If you did not request this, ignore this email.</p>"
            ),
            "text": (
                "Reset your Insolvencys Radar password using this link: "
                f"{reset_url}\nThis link expires in 30 minutes."
            ),
        },
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Resend HTTP {response.status_code}; "
            f"body={response.text!r}; "
            f"request_id={response.headers.get('x-resend-id')}; "
            f"content_type={response.headers.get('content-type')}"
        )
