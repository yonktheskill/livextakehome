"""Explicitly enabled demo account provisioning; never resets an existing account."""

from app.core.security import passwords
from app.db.models import Conversation, User, db

TEST_ACCOUNTS = (
    ("test@testingmail.com", "testtesttest"),
    ("test2@testingmail.com", "testtesttest2"),
)


def seed_test_account():
    with db.atomic("IMMEDIATE"):
        for email, password in TEST_ACCOUNTS:
            if User.get_or_none(User.email == email):
                continue
            user = User.create(email=email, password_hash=passwords.hash(password))
            Conversation.create(user=user)
