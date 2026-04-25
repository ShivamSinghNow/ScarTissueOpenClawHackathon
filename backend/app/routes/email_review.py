from __future__ import annotations

import html
import os
import sys

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.schemas import Warning, _sanitize_pr_url

router = APIRouter()


_SEVERITY_COLORS = {
    "high":   ("#ef4444", "rgba(239,68,68,0.10)",  "rgba(239,68,68,0.28)"),
    "medium": ("#f59e0b", "rgba(245,158,11,0.10)", "rgba(245,158,11,0.28)"),
    "low":    ("#6a9a6a", "rgba(106,154,106,0.10)", "rgba(106,154,106,0.28)"),
}


class EmailReviewRequest(BaseModel):
    pr_url: str
    pr_title: str
    pr_repo: str
    pr_author: str
    recipient_email: EmailStr
    warnings: list[Warning]
    dry_run: bool = False

    @field_validator("pr_url", mode="before")
    @classmethod
    def strip_invisible(cls, v: str) -> str:
        return _sanitize_pr_url(v)


class EmailReviewResponse(BaseModel):
    pr_url: str
    recipient: str
    inbox_id: str | None = None
    inbox_address: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    warnings_sent: int
    subject: str
    dry_run: bool = False
    html_preview: str | None = Field(default=None, description="Only populated on dry_run")


def _subject(req: EmailReviewRequest) -> str:
    n = len(req.warnings)
    if n == 0:
        return f"[ScarTissue] Clean review on your PR: {req.pr_title}"
    return f"[ScarTissue] {n} warning{'s' if n != 1 else ''} on your PR: {req.pr_title}"


def _commit_link(repo_slug: str, sha: str) -> str:
    return f"https://github.com/{repo_slug}/commit/{sha}"


def _render_text(req: EmailReviewRequest) -> str:
    n = len(req.warnings)
    lines = [
        f"Hi {req.pr_author},",
        "",
        f"ScarTissue reviewed your PR \"{req.pr_title}\" ({req.pr_url})",
        f"against historical bug fixes in {req.pr_repo} and found "
        f"{n} potential regression pattern{'s' if n != 1 else ''}.",
        "",
    ]
    for i, w in enumerate(req.warnings, 1):
        lines.append(f"--- Warning {i}/{n} [{w.severity.upper()} | {w.confidence:.0%}] ---")
        lines.append(f"File: {w.pr_file}")
        lines.append(f"Hunk: {w.pr_hunk}")
        lines.append("")
        lines.append(f"Why this matters: {w.explanation}")
        if w.matched_incident:
            inc = w.matched_incident
            first_line = inc.commit_message.splitlines()[0] if inc.commit_message else "(no commit message)"
            lines.append("")
            lines.append(f"Rhymes with commit {inc.commit_sha[:7]}:")
            lines.append(f"  > {first_line}")
            lines.append(f"  {_commit_link(req.pr_repo, inc.commit_sha)}")
        if w.proposed_fix:
            lines.append("")
            lines.append("Suggested fix:")
            for fix_line in w.proposed_fix.splitlines():
                lines.append(f"  {fix_line}")
        lines.append("")
    lines.append(f"View the PR: {req.pr_url}")
    lines.append("")
    lines.append("— ScarTissue (every codebase remembers its bugs)")
    return "\n".join(lines)


def _render_html(req: EmailReviewRequest) -> str:
    n = len(req.warnings)
    esc = html.escape
    cards: list[str] = []
    for i, w in enumerate(req.warnings, 1):
        sev_color, sev_bg, sev_border = _SEVERITY_COLORS.get(w.severity, _SEVERITY_COLORS["low"])
        incident_block = ""
        if w.matched_incident:
            inc = w.matched_incident
            first_line = inc.commit_message.splitlines()[0] if inc.commit_message else "(no commit message)"
            short = esc(inc.commit_sha[:7])
            link = _commit_link(req.pr_repo, inc.commit_sha)
            incident_block = (
                f'<div style="margin-top:10px;padding:10px 12px;background:#fafafa;border-left:3px solid #d4d4d8;border-radius:4px;">'
                f'<div style="font-size:12px;color:#71717a;margin-bottom:4px;">Rhymes with commit '
                f'<a href="{esc(link)}" style="color:#3f3f46;text-decoration:none;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{short}</a></div>'
                f'<div style="font-size:13px;color:#27272a;font-style:italic;">{esc(first_line)}</div>'
                f"</div>"
            )
        fix_block = ""
        if w.proposed_fix:
            fix_block = (
                f'<div style="margin-top:10px;">'
                f'<div style="font-size:12px;color:#71717a;margin-bottom:4px;font-weight:600;">Suggested fix</div>'
                f'<pre style="margin:0;padding:10px 12px;background:#0f172a;color:#e2e8f0;border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.5;overflow-x:auto;white-space:pre-wrap;">{esc(w.proposed_fix)}</pre>'
                f"</div>"
            )
        cards.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="margin:0 0 14px;border:1px solid #e4e4e7;border-radius:8px;border-collapse:separate;">'
            f'<tr><td style="padding:14px 16px;">'
            f'<div style="display:inline-block;padding:2px 8px;background:{sev_bg};border:1px solid {sev_border};color:{sev_color};border-radius:4px;font-size:11px;font-weight:700;letter-spacing:0.04em;">'
            f'{esc(w.severity.upper())} · {w.confidence:.0%}'
            f'</div>'
            f'<div style="margin-top:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;color:#52525b;">{esc(w.pr_file)}</div>'
            f'<div style="margin-top:2px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#a1a1aa;overflow:hidden;text-overflow:ellipsis;">{esc(w.pr_hunk)}</div>'
            f'<div style="margin-top:10px;font-size:14px;color:#27272a;line-height:1.55;">{esc(w.explanation)}</div>'
            f'{incident_block}'
            f'{fix_block}'
            f"</td></tr></table>"
        )

    headline = (
        f"ScarTissue found <strong>{n} potential regression pattern{'s' if n != 1 else ''}</strong>"
        if n > 0
        else "ScarTissue found <strong>no historical regression patterns</strong>"
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(_subject(req))}</title></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#18181b;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f4f5;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:640px;background:#ffffff;border:1px solid #e4e4e7;border-radius:10px;border-collapse:separate;">
<tr><td style="padding:22px 22px 6px;">
<div style="font-size:11px;font-weight:700;letter-spacing:0.12em;color:#a1a1aa;">SCARTISSUE · PR REVIEW</div>
<h1 style="margin:6px 0 4px;font-size:20px;line-height:1.3;color:#18181b;font-weight:600;">{esc(req.pr_title)}</h1>
<div style="font-size:13px;color:#71717a;">
<span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(req.pr_repo)}</span>
 · by <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(req.pr_author)}</span>
</div>
</td></tr>
<tr><td style="padding:14px 22px 4px;font-size:14px;color:#27272a;line-height:1.55;">
<p style="margin:0 0 10px;">Hi {esc(req.pr_author)},</p>
<p style="margin:0 0 6px;">{headline} on your PR after cross-referencing it against historical bug fixes in <span style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(req.pr_repo)}</span>.</p>
</td></tr>
<tr><td style="padding:12px 22px 4px;">
{''.join(cards) if cards else '<div style="padding:14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;color:#166534;font-size:13px;">No regression patterns matched.</div>'}
</td></tr>
<tr><td style="padding:8px 22px 22px;">
<a href="{esc(req.pr_url)}" style="display:inline-block;padding:10px 16px;background:#18181b;color:#ffffff;text-decoration:none;border-radius:6px;font-size:13px;font-weight:500;">View PR on GitHub →</a>
</td></tr>
<tr><td style="padding:14px 22px 18px;border-top:1px solid #f4f4f5;font-size:11px;color:#a1a1aa;line-height:1.5;">
Sent by <a href="https://github.com/ShivamSinghNow/scartissue" style="color:#71717a;text-decoration:none;">ScarTissue</a> via <a href="https://agentmail.to" style="color:#71717a;text-decoration:none;">AgentMail</a>. Every codebase remembers its bugs.
</td></tr>
</table>
</td></tr></table>
</body></html>"""


@router.post("/email-review", response_model=EmailReviewResponse)
async def email_review(request: EmailReviewRequest) -> EmailReviewResponse:
    subject = _subject(request)
    text_body = _render_text(request)
    html_body = _render_html(request)

    if request.dry_run:
        return EmailReviewResponse(
            pr_url=request.pr_url,
            recipient=request.recipient_email,
            warnings_sent=len(request.warnings),
            subject=subject,
            dry_run=True,
            html_preview=html_body,
        )

    api_key = os.getenv("AGENTMAIL_API_KEY")
    if not api_key:
        raise HTTPException(400, "AGENTMAIL_API_KEY not configured")

    from agentmail import AsyncAgentMail
    from agentmail.inboxes.types import CreateInboxRequest

    client = AsyncAgentMail(api_key=api_key)
    client_id = os.getenv("AGENTMAIL_INBOX_CLIENT_ID", "scartissue-pr-bot")

    try:
        inbox = await client.inboxes.create(request=CreateInboxRequest(client_id=client_id))
        sent = await client.inboxes.messages.send(
            inbox.inbox_id,
            to=request.recipient_email,
            subject=subject,
            text=text_body,
            html=html_body,
        )
    except Exception as exc:
        print(f"email-review failed: {exc}", file=sys.stderr)
        raise HTTPException(502, f"AgentMail send failed: {exc}") from exc

    return EmailReviewResponse(
        pr_url=request.pr_url,
        recipient=request.recipient_email,
        inbox_id=inbox.inbox_id,
        inbox_address=inbox.email,
        message_id=sent.message_id,
        thread_id=sent.thread_id,
        warnings_sent=len(request.warnings),
        subject=subject,
    )
