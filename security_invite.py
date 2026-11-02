"""Isolated staff-security invitation transport for Hobby Bot (no credentials logged)."""
import re
from urllib.parse import urlsplit

SNOWFLAKE = re.compile(r"^[0-9]{15,22}$")
TOKEN_LINK = re.compile(r"^/staff-security/#invite=[A-Za-z0-9_-]{43}$")

def eligible(actor_role_ids, target_role_ids, *, staff_role, admin_role, owner_role):
    if not all(SNOWFLAKE.fullmatch(str(x or "")) for x in (staff_role, admin_role, owner_role)):
        return False
    managers = {str(admin_role), str(owner_role)}
    members = managers | {str(staff_role)}
    return bool(managers.intersection(map(str, actor_role_ids))) and bool(
        members.intersection(map(str, target_role_ids))
    )

def issue_invitation(*, post, api_url, public_origin, bot_key,
                     guild_id, actor_id, target_id, actor_roles):
    """Return the private one-time link only; never log response/secret.

    The backend must revalidate Timeclock links and exact invitation grants.
    This function only requests an invitation; it never grants MFA enrollment.
    """
    if not (api_url.startswith("https://") or api_url.startswith("http://localhost")):
        raise ValueError("Security backend must use HTTPS or isolated localhost.")
    if len(bot_key.encode("utf-8")) < 32:
        raise ValueError("Dedicated invitation bot secret is not configured.")
    expected = urlsplit(public_origin)
    if expected.scheme != "https" and not (
        expected.scheme == "http" and expected.hostname == "localhost"
    ):
        raise ValueError("Security invitation origin must use HTTPS.")
    if not expected.hostname or expected.path or expected.query or expected.fragment:
        raise ValueError("Security invitation origin must not include path or query.")
    if not all(SNOWFLAKE.fullmatch(str(x or "")) for x in (guild_id, actor_id, target_id)):
        raise ValueError("Discord identifiers are invalid.")
    response = post(
        api_url.rstrip("/") + "/staff-enrollment-invites/bot/issue",
        headers={"x-hc-invitation-bot-key":bot_key, "Content-Type":"application/json"},
        json={"guildId":str(guild_id),"actorDiscordId":str(actor_id),
              "targetDiscordId":str(target_id),
              "roleIds":[str(r) for r in actor_roles]},
        timeout=10
    )
    if response.status_code != 201:
        raise ValueError("Unable to authorize or create this invitation.")
    data = response.json()
    link = data.get("link") if isinstance(data, dict) else None
    if not isinstance(link, str):
        raise ValueError("Invalid invitation response.")
    parts = urlsplit(link)
    if (parts.scheme != expected.scheme or parts.netloc != expected.netloc or
        not TOKEN_LINK.fullmatch(parts.path+"#"+parts.fragment) or
        parts.username or parts.password or parts.query):
        raise ValueError("Unexpected enrollment invitation destination.")
    return link
