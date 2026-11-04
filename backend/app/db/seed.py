"""Explicitly enabled demo account provisioning; never resets an existing account."""

from app.core.security import passwords
from app.db.models import Conversation, User, db

TEST_EMAIL = "test@testingmail.com"


def seed_test_account():
    with db.atomic("IMMEDIATE"):
        if User.get_or_none(User.email == TEST_EMAIL):
            return
        user = User.create(email=TEST_EMAIL, password_hash=passwords.hash("testtesttest"))
        Conversation.create(user=user)
