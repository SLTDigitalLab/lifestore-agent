"""Username/password test account, hashed passwords and revocable cookie sessions."""
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import timedelta
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import UserAccount, LoginSession, AuthAttempt, utcnow
from app.db.session import get_engine

router = APIRouter(prefix='/api/auth')
COOKIE = '__Host-lifestore-session'
SESSION_SECONDS = 30 * 24 * 60 * 60


@lru_cache(maxsize=1)
def account_engine():
    return get_engine()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    # OWASP scrypt configuration: 16 MiB and parallelization cost 5.
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=5,
                            maxmem=64 * 1024 * 1024).hex()
    return 'scrypt$16384$8$5$' + salt + '$' + digest


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split('$')
        if (n, r, p) != ('16384', '8', '5'):
            return False
        return hmac.compare_digest(hash_password(password, salt), stored)
    except (ValueError, TypeError):
        return False


def check_write(request: Request):
    # Custom header + no CORS prevents cross-origin form/fetch mutations.
    if request.headers.get('x-lifestore-request') != '1':
        raise HTTPException(403, 'Please use the LifeStore website.')
    origin = request.headers.get('origin')
    expected = os.getenv('PAYHERE_PUBLIC_BASE_URL', str(request.base_url)).rstrip('/')
    if origin and origin != expected:
        raise HTTPException(403, 'Invalid request origin.')


def current_user(request: Request):
    token = request.cookies.get(COOKIE, '')
    if not token:
        raise HTTPException(401, 'Sign in to continue.')
    digest = hashlib.sha256(token.encode()).hexdigest()
    with Session(account_engine()) as db:
        login = db.get(LoginSession, digest)
        if not login or login.expires_at <= utcnow():
            raise HTTPException(401, 'Your session expired. Please sign in again.')
        user = db.get(UserAccount, login.user_id)
        if not user:
            raise HTTPException(401, 'Sign in to continue.')
        return {'id': user.id, 'name': user.name, 'username': user.username}


def throttle(request, username):
    ip = request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
    window = int(time.time()) // 900
    limited = False
    with Session(account_engine()) as db, db.begin():
        for source, limit in [('ip:' + ip, 40), ('username:' + username, 12)]:
            key = hashlib.sha256(source.encode()).hexdigest()
            lock = int.from_bytes(bytes.fromhex(key)[:8], 'big', signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
            row = db.get(AuthAttempt, key)
            if row is None:
                row = AuthAttempt(key=key, window=window, count=0)
                db.add(row)
            if row.window != window:
                row.window, row.count = window, 0
            row.count += 1
            limited |= row.count > limit
    if limited:
        raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator('username')
    @classmethod
    def normalize_username(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[a-z0-9._-]{3,64}', value):
            raise ValueError('Enter a valid username')
        return value


def issue_session(db, user, response):
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                        expires_at=utcnow() + timedelta(seconds=SESSION_SECONDS)))
    db.execute(delete(LoginSession).where(LoginSession.expires_at < utcnow()))
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                        httponly=True, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return {'user': {'id': user.id, 'name': user.name, 'username': user.username}}


@router.post('/login', dependencies=[Depends(check_write)])
def login(data: Credentials, request: Request, response: Response):
    throttle(request, data.username)
    with Session(account_engine()) as db, db.begin():
        user = db.scalar(select(UserAccount).where(UserAccount.username == data.username))
        if not user:
            hash_password(data.password)
            raise HTTPException(401, 'Username or password is incorrect.')
        if not verify_password(data.password, user.password_hash):
            raise HTTPException(401, 'Username or password is incorrect.')
        return issue_session(db, user, response)


@router.get('/me')
def me(response: Response, user=Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    return {'user': user}


@router.post('/logout', dependencies=[Depends(check_write)])
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE, '')
    with Session(account_engine()) as db, db.begin():
        db.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax', path='/')
    return {'ok': True}
