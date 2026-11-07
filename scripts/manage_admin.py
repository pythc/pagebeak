"""Operator-only administrator bootstrap. No API/token may grant this role."""

import argparse

from akb.config import Settings
from akb.models import User
from sqlalchemy import create_engine, select, update

parser = argparse.ArgumentParser()
parser.add_argument("--email", required=True)
parser.add_argument("--revoke", action="store_true")
args = parser.parse_args()
engine = create_engine(Settings().migration_database_url)
try:
    with engine.begin() as connection:
        user_id = connection.scalar(select(User.id).where(User.email == args.email.strip().casefold()))
        if not user_id:
            raise SystemExit("User not found; register a browser account first.")
        connection.execute(update(User).where(User.id == user_id).values(is_admin=not args.revoke))
    print(
        "Administrator role revoked." if args.revoke else "Administrator role granted; refresh the browser."
    )
finally:
    engine.dispose()
