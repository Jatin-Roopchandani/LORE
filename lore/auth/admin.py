"""The waitlist (public) and the admin API (admin-only).

Admins are `users.role = 'admin'`, granted from the server's shell with
`lore admin grant <email>`: there is no web path to become one. Every admin
route is behind `require_admin` and, being a POST, behind the CSRF guard.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lore.auth import accounts, ratelimit
from lore.auth.current import AuthContext, session_user
from lore.auth.mailer import MailError, invite_message
from lore.auth.signin import client_ip
from lore.central.models import User, Waitlist

log = logging.getLogger("lore.auth")

WAITLIST_PER_IP_PER_HOUR = 10
MAX_READING_CHARS = 500
PENDING, INVITED, DECLINED = "pending", "invited", "declined"


class WaitlistBody(BaseModel):
    email: str = ""
    reading: str | None = None
    consent: bool = False
    # A field real people never see (the form hides it); bots fill every field.
    website: str = ""


class InviteBody(BaseModel):
    days: int = Field(default=14, ge=1, le=90)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status)


def _entry(row: Waitlist) -> dict:
    return {"id": row.id, "email": row.email, "reading": row.reading,
            "status": row.status, "created_at": row.created_at.isoformat(),
            "invited_at": row.invited_at.isoformat() if row.invited_at else None}


def build_admin_router(ctx: AuthContext) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings

    def require_admin(request: Request) -> str:
        """The admin's user id; 401 if signed out, 403 for anyone else."""
        user = session_user(request)
        if user is None:
            raise HTTPException(401, "sign in required")
        with ctx.sessions() as db:
            row = db.get(User, user.id)
        if row is None or row.role != "admin" or row.status != "active":
            raise HTTPException(403, "admins only")
        return row.id

    # --- the public waitlist -------------------------------------------

    @router.post("/api/waitlist", status_code=202)
    def join_waitlist(body: WaitlistBody, request: Request):
        """Ask for an invite. Always 202, whatever happened (stored, a
        duplicate, a honeypot, no consent, a bad address, rate limited), so a
        bot learns nothing from the answer."""
        accepted = {"ok": True}
        email = accounts.normalize_email(body.email)
        if body.website or not body.consent or email is None:
            return accepted
        now = ctx.now()
        with ctx.sessions() as db:
            if not ratelimit.allow(db, "waitlist", client_ip(request, settings.trust_proxy),
                                   WAITLIST_PER_IP_PER_HOUR, now):
                db.commit()
                return accepted
            known = (accounts.user_by_email(db, email) is not None
                     or db.scalars(sa.select(Waitlist).where(
                         Waitlist.email == email)).first() is not None)
            if not known:
                reading = (body.reading or "").strip()[:MAX_READING_CHARS] or None
                db.add(Waitlist(email=email, reading=reading, created_at=now,
                                status=PENDING))
            db.commit()
        return accepted

    # --- admin ---------------------------------------------------------

    @router.get("/api/admin/waitlist")
    def list_waitlist(_admin: str = Depends(require_admin)):
        with ctx.sessions() as db:
            rows = db.scalars(sa.select(Waitlist).order_by(
                (Waitlist.status != PENDING), Waitlist.created_at)).all()
        return {"entries": [_entry(r) for r in rows]}

    @router.post("/api/admin/waitlist/{entry_id}/approve")
    def approve(entry_id: int, admin: str = Depends(require_admin)):
        now = ctx.now()
        with ctx.sessions() as db:
            row = db.get(Waitlist, entry_id)
            if row is None:
                return _error(404, "not_found", "no such request")
            if row.status == INVITED:
                return _error(409, "already_invited", "this person was already invited")
            code = accounts.create_invite(db, admin, now)
            row.status, row.invited_at = INVITED, now
            subject, text, html = invite_message(f"{settings.origin}/signin?invite={code}")
            try:  # send before committing: no mail, no invite
                ctx.mailer.send(to=row.email, subject=subject, text=text, html=html)
            except MailError as exc:
                db.rollback()
                log.warning("invite email not sent: %s", exc)
                return _error(502, "mail_failed",
                              "the invitation email could not be sent; nothing was changed")
            db.commit()
            return {"invited": True, "entry": _entry(row)}

    @router.post("/api/admin/waitlist/{entry_id}/decline")
    def decline(entry_id: int, _admin: str = Depends(require_admin)):
        with ctx.sessions() as db:
            row = db.get(Waitlist, entry_id)
            if row is None:
                return _error(404, "not_found", "no such request")
            if row.status == INVITED:
                return _error(409, "already_invited", "this person was already invited")
            row.status = DECLINED
            db.commit()
            return {"declined": True, "entry": _entry(row)}

    @router.post("/api/admin/invites", status_code=201)
    def create_invite(body: InviteBody | None = None,
                      admin: str = Depends(require_admin)):
        """A code to hand out directly. It is shown once, here: only its hash
        is stored."""
        now = ctx.now()
        days = (body or InviteBody()).days
        with ctx.sessions() as db:
            code = accounts.create_invite(db, admin, now, timedelta(days=days))
            db.commit()
        return {"code": code, "link": f"{settings.origin}/signin?invite={code}",
                "expires_at": (now + timedelta(days=days)).isoformat()}

    @router.get("/api/admin/users")
    def list_users(_admin: str = Depends(require_admin)):
        from lore.central.models import ApiKey, Usage
        from lore.orchestrator.gemini_budget import today
        day = today(ctx.now())
        with ctx.sessions() as db:
            rows = db.scalars(sa.select(User).order_by(User.created_at)).all()
            with_key = set(db.scalars(sa.select(ApiKey.user_id)))
            used = dict(db.execute(
                sa.select(Usage.user_id, sa.func.sum(Usage.requests))
                .where(Usage.day == day).group_by(Usage.user_id)).all())
        return {"users": [
            {"id": u.id, "email": u.email, "name": u.name, "role": u.role,
             "status": u.status, "created_at": u.created_at.isoformat(),
             # Whether they have saved a Gemini key, and today's requests.
             "has_key": u.id in with_key, "requests_today": int(used.get(u.id) or 0)}
            for u in rows]}

    return router
