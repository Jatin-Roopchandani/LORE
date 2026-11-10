"""The waitlist (public) and the admin API (admin-only).

Admins are `users.role = 'admin'`, granted from the server's shell with
`lore admin grant <email>`: there is no web path to become one. Every admin
route is behind `require_admin` and, being a POST, behind the CSRF guard.

Release step 6: admins disable and re-enable users, revoke a user's
sessions and delete an account (lore.central.erase), and every admin action
is written to `admin_events` (who, what, to whom; never an email address or
a secret). GET /api/admin/events lists them.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lore.auth import accounts, ratelimit
from lore.auth.current import AuthContext, session_user
from lore.auth.mailer import MailError, invite_message
from lore.auth.signin import client_ip
from lore.central.models import AdminEvent, User, Waitlist

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
    # Bind the code to one address (the sign-in must prove it); none: anyone.
    email: str | None = Field(default=None, max_length=320)


def log_event(db, admin: str | None, action: str, target: str | None = None,
              detail: dict | None = None, *, now=None) -> None:
    """One admin action, in the caller's transaction."""
    from lore.central.models import utcnow
    db.add(AdminEvent(at=now or utcnow(), admin_id=admin, action=action,
                      target_user_id=target, detail=detail))


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

    def notify(email: str, reading: str | None) -> None:
        """The owner's note of a new request; best effort, after the answer is sent."""
        text = (f"{email} asked for an invite to LORE.\n\n"
                f"Reading: {reading or '(not given)'}\n\n"
                f"Approve or decline: {settings.origin}/admin\n")
        try:
            ctx.mailer.send(to=settings.notify_email,
                            subject=f"LORE invite request: {email}", text=text)
        except MailError as exc:
            log.warning("waitlist notification not sent: %s", exc)

    @router.post("/api/waitlist", status_code=202)
    def join_waitlist(body: WaitlistBody, request: Request, background: BackgroundTasks):
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
        if not known and settings.notify_email:
            # Sent after the response, so a new request answers as fast as a duplicate.
            background.add_task(notify, email, reading)
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
            # The code works only for the address that asked (release step 6).
            code = accounts.create_invite(db, admin, now, email=row.email)
            row.status, row.invited_at = INVITED, now
            log_event(db, admin, "approve", detail={"waitlist_id": row.id}, now=now)
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
            log_event(db, _admin, "decline", detail={"waitlist_id": row.id}, now=ctx.now())
            db.commit()
            return {"declined": True, "entry": _entry(row)}

    @router.post("/api/admin/invites", status_code=201)
    def create_invite(body: InviteBody | None = None,
                      admin: str = Depends(require_admin)):
        """A code to hand out directly. It is shown once, here: only its hash
        is stored."""
        now = ctx.now()
        body = body or InviteBody()
        days = body.days
        email = None
        if body.email:
            email = accounts.normalize_email(body.email)
            if email is None:
                return _error(400, "invalid_email", "that does not look like an email address")
        with ctx.sessions() as db:
            code = accounts.create_invite(db, admin, now, timedelta(days=days), email=email)
            log_event(db, admin, "create_invite",
                      detail={"days": days, "bound": email is not None}, now=now)
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

    # --- reported answers and their traces (release step 4) ------------

    @router.get("/api/admin/reports")
    def list_reports(_admin: str = Depends(require_admin)):
        from lore.central.models import Report, Trace
        from lore.web.history import report_out
        with ctx.sessions() as db:
            rows = db.execute(
                sa.select(Report, User.email, Trace.question)
                .join(User, User.id == Report.user_id)
                .outerjoin(Trace, Trace.request_id == Report.request_id)
                .order_by(Report.created_at.desc(), Report.id.desc()).limit(500)).all()
        return JSONResponse({"reports": [
            {**report_out(r), "user_id": r.user_id, "email": email, "question": question,
             "trace": question is not None}
            for r, email, question in rows]}, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/reports/{report_id}/resolve")
    def resolve_report(report_id: int, _admin: str = Depends(require_admin)):
        from lore.central.models import Report
        from lore.web.history import report_out
        with ctx.sessions() as db:
            row = db.get(Report, report_id)
            if row is None:
                return _error(404, "not_found", "no such report")
            row.status = "resolved"
            log_event(db, _admin, "resolve_report", row.user_id, {"report_id": row.id},
                      now=ctx.now())
            db.commit()
            return {"report": report_out(row)}

    @router.get("/api/admin/traces/{request_id}")
    def get_trace(request_id: str, _admin: str = Depends(require_admin)):
        from lore.central import traces
        with ctx.sessions() as db:
            row = traces.get(db, request_id)
            if row is None:
                return _error(404, "not_found", "no such trace (kept 30 days)")
            out = traces.trace_out(row)
            # Reading someone's trace is an admin action too.
            log_event(db, _admin, "view_trace", row.user_id, {"request_id": request_id},
                      now=ctx.now())
            db.commit()
            return JSONResponse(out, headers={"Cache-Control": "no-store"})

    # --- user controls (release step 6) --------------------------------

    def _target(db, user_id: str, admin: str):
        row = db.get(User, user_id)
        if row is None:
            return None, _error(404, "not_found", "no such user")
        if row.id == admin:
            return None, _error(409, "self_action",
                                "use another admin account to change your own")
        return row, None

    def _user_out(row: User) -> dict:
        return {"id": row.id, "status": row.status, "role": row.role}

    @router.post("/api/admin/users/{user_id}/disable")
    def disable_user(user_id: str, admin: str = Depends(require_admin)):
        """Refuse sign-in, end every session at once, pause queued jobs."""
        from lore.auth.sessions import delete_user_sessions
        from lore.central import queue
        with ctx.sessions() as db:
            row, refused = _target(db, user_id, admin)
            if refused is not None:
                return refused
            if row.status == "deleting":
                return _error(409, "busy", "this account is being deleted")
            row.status = "disabled"
            delete_user_sessions(db, user_id)
            log_event(db, admin, "disable", user_id, now=ctx.now())
            db.commit()
            queue.pause_user(db, user_id, outcome="user_disabled")  # commits
            return {"user": _user_out(db.get(User, user_id))}

    @router.post("/api/admin/users/{user_id}/enable")
    def enable_user(user_id: str, admin: str = Depends(require_admin)):
        from lore.central import queue
        with ctx.sessions() as db:
            row, refused = _target(db, user_id, admin)
            if refused is not None:
                return refused
            if row.status == "deleting":
                return _error(409, "busy", "this account is being deleted")
            row.status = "active"
            log_event(db, admin, "enable", user_id, now=ctx.now())
            db.commit()
            queue.resume_user(db, user_id)  # commits
            return {"user": _user_out(db.get(User, user_id))}

    @router.post("/api/admin/users/{user_id}/revoke-sessions")
    def revoke_sessions(user_id: str, admin: str = Depends(require_admin)):
        from lore.auth.sessions import delete_user_sessions
        from lore.central.models import AuthSession
        with ctx.sessions() as db:
            row, refused = _target(db, user_id, admin)
            if refused is not None:
                return refused
            count = db.scalar(sa.select(sa.func.count()).select_from(AuthSession)
                              .where(AuthSession.user_id == user_id))
            delete_user_sessions(db, user_id)
            log_event(db, admin, "revoke_sessions", user_id, {"sessions": count},
                      now=ctx.now())
            db.commit()
            return {"revoked": count}

    @router.delete("/api/admin/users/{user_id}")
    def delete_user(user_id: str, admin: str = Depends(require_admin)):
        """Delete the account and all its data (lore.central.erase). Safe to
        repeat: an unfinished deletion carries on."""
        from lore.central.erase import erase_user
        from lore.tenancy import InvalidUserId, validate_user_id
        try:
            validate_user_id(user_id)
        except InvalidUserId:
            return _error(404, "not_found", "no such user")
        with ctx.sessions() as db:
            row, refused = _target(db, user_id, admin)
            if refused is not None:
                return refused
        done = erase_user(ctx.sessions, ctx.data_root, user_id, now=ctx.now())
        with ctx.sessions() as db:
            log_event(db, admin, "delete_user", user_id,
                      {"library_removed": done.library_removed}, now=ctx.now())
            db.commit()
        return {"deleted": True, "library_removed": done.library_removed}

    @router.get("/api/admin/events")
    def list_events(_admin: str = Depends(require_admin)):
        with ctx.sessions() as db:
            rows = db.execute(
                sa.select(AdminEvent, User.email)
                .outerjoin(User, User.id == AdminEvent.admin_id)
                .order_by(AdminEvent.at.desc(), AdminEvent.id.desc()).limit(200)).all()
        return JSONResponse({"events": [
            {"id": e.id, "at": e.at.isoformat(), "admin_id": e.admin_id, "admin": email,
             "action": e.action, "target_user_id": e.target_user_id, "detail": e.detail}
            for e, email in rows]}, headers={"Cache-Control": "no-store"})

    return router
