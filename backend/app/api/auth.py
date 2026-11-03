import time

from fastapi import APIRouter, HTTPException, Request, Response
from peewee import IntegrityError
from pydantic import BaseModel, EmailStr, Field

from app.core.security import digest, passwords, token, verify_password
from app.db.database import query
from app.db.models import Conversation, Session, User, db

router = APIRouter(prefix="/api/auth")
COOKIE = "cal_session"


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)


def public(user):
    return {
        "id": user.id,
        "email": user.email,
        "time_zone": user.time_zone,
        "preferred_name": user.preferred_name,
    }


def current_sync(raw):
    session = (
        Session.get_or_none((Session.token_hash == digest(raw)) & (Session.expires > time.time()))
        if raw
        else None
    )
    if not session:
        raise HTTPException(401, "Sign in to continue.")
    return session.user


async def current(request: Request):
    return await query(current_sync, request.cookies.get(COOKIE))


async def sign_in(request, response, user):
    raw = token()

    def persist():
        with db.atomic("IMMEDIATE"):
            old = request.cookies.get(COOKIE)
            if old:
                Session.delete().where(Session.token_hash == digest(old)).execute()
            Session.create(
                token_hash=digest(raw),
                user=user.id,
                expires=time.time() + request.app.state.settings.session_days * 86400,
            )

    await query(persist)
    response.set_cookie(
        COOKIE,
        raw,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="lax",
        max_age=request.app.state.settings.session_days * 86400,
        path="/api",
    )
    return public(user)


@router.post("/register")
async def register(data: Credentials, request: Request, response: Response):
    encoded = await query(passwords.hash, data.password)

    def create():
        try:
            with db.atomic("IMMEDIATE"):
                user = User.create(email=str(data.email).lower(), password_hash=encoded)
                Conversation.create(user=user)
                return user
        except IntegrityError:
            raise HTTPException(409, "An account with this email already exists.") from None

    return await sign_in(request, response, await query(create))


@router.post("/login")
async def login(data: Credentials, request: Request, response: Response):
    user = await query(lambda: User.get_or_none(User.email == str(data.email).lower()))
    # Dummy work also makes an unknown email take an Argon2 operation.
    valid = await query(verify_password, user.password_hash, data.password) if user else False
    if not user:
        await query(passwords.hash, data.password)
    if not valid:
        raise HTTPException(401, "Email or password is incorrect.")
    if passwords.check_needs_rehash(user.password_hash):
        user.password_hash = await query(passwords.hash, data.password)
        await query(user.save)
    return await sign_in(request, response, user)


@router.get("/me")
async def me(request: Request):
    return public(await current(request))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    raw = request.cookies.get(COOKIE, "")
    await query(lambda: Session.delete().where(Session.token_hash == digest(raw)).execute())
    response.delete_cookie(COOKIE, path="/api")
