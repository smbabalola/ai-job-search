"""Create the first staff ADMIN (Bundle 7 spec A6): a dedicated user with no
customer account, the ADMIN role, and a TOTP secret whose provisioning URI is
printed once. The enrolment is confirmed by the first code at /admin/login.

    python -m webapp.tools.create_admin --email ops@example.com
    (the password is read from JOBSEARCH_ADMIN_PASSWORD or prompted for)"""
from __future__ import annotations

import argparse
import getpass
import os
from datetime import datetime, timezone

from webapp.config import Settings
from webapp.persistence.audit import audit
from webapp.persistence.db import connect, init_db
from webapp.services import staff_auth
from webapp.services.passwords import hash_password, password_problems


def create_admin(email: str, password: str, *, settings: Settings | None = None, display_name: str = "Admin",
                 out=print) -> str:
    settings = settings or Settings()
    problems = password_problems(password)
    if problems:
        raise ValueError(" ".join(problems))
    init_db(settings)
    now = datetime.now(timezone.utc)
    conn = connect(settings)
    try:
        user_id = staff_auth.create_staff_user(conn, email=email, password_hash=hash_password(password),
                                               display_name=display_name, now=now)
        staff_auth.grant_role(conn, user_id=user_id, role="ADMIN", actor_user_id=None, reason="create_admin CLI",
                              now=now)
        secret = staff_auth.start_totp_enrolment(conn, settings, user_id)
        audit(conn, actor_type="SYSTEM", actor_id="create_admin", account_id=None, action="STAFF_ROLE_CHANGED",
              now=now, target_type="user", target_id=user_id, detail={"role": "ADMIN", "action": "GRANT"},
              secret=settings.secret_key)
        conn.commit()
    finally:
        conn.close()
    out("Staff admin created. Add this to an authenticator app now; it is not shown again:")
    out(staff_auth.provisioning_uri(secret, email))
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Admin")
    args = parser.parse_args(argv)
    password = os.environ.get("JOBSEARCH_ADMIN_PASSWORD") or getpass.getpass("Password (12+ characters): ")
    create_admin(args.email, password, display_name=args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
