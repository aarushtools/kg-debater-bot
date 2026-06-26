import logging
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from math import ceil
from typing import Any
from urllib.parse import urlencode

import hikari
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi_discord import DiscordOAuthClient, Unauthorized
from hikari.impl.rest import RESTApp
from PIL import Image, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist, IntegrityError, ValidationError
from tortoise.transactions import in_transaction

from helpers import get_match_score_nickname, sync_tier_roles
from models import AdminAction, IncompleteMatch, Match, Tier, User
import secret


SESSION_MAX_AGE = 60 * 60 * 8
PAGE_SIZE = 25

templates = Jinja2Templates(directory="templates")
discord = DiscordOAuthClient(
    secret.CLIENT_ID,
    secret.CLIENT_SECRET,
    secret.DISCORD_OAUTH_REDIRECT_URI,
    scopes=("identify",),
)

logging.basicConfig(
    filename="app_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    rest_app = RESTApp()
    await rest_app.start()
    if not Tortoise.is_inited():
        await Tortoise.init(config=secret.TORTOISE_ORM, _enable_global_fallback=True)

    app.state.discord_rest_app = rest_app
    try:
        yield
    finally:
        await Tortoise.close_connections()
        await rest_app.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=secret.WEB_SESSION_SECRET or secret.CLIENT_SECRET,
    session_cookie="kg_admin_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=True,
)


def get_logged_in_user(request: Request) -> dict[str, Any] | None:
    user = request.session.get("user")
    if isinstance(user, dict):
        return user

    return None


def require_admin_user(request: Request) -> dict[str, Any]:
    user = get_logged_in_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )

    return user


def admin_redirect(path: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error

    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"{path}{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def none_if_empty(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, str) and not value.strip():
        return None

    return value


def form_bool(form: Any, key: str) -> bool:
    return form.get(key) in {"on", "true", "1", "yes"}


def parse_datetime(value: Any) -> datetime | None:
    value = none_if_empty(value)
    if value is None:
        return None

    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    return datetime.fromisoformat(text)


async def tier_choices() -> list[Tier]:
    return await Tier.all().order_by("elo_min", "name")


async def user_choices() -> list[User]:
    return await User.all().order_by("discord_name")


async def incomplete_match_choices() -> list[IncompleteMatch]:
    return await IncompleteMatch.all().order_by("-started_at", "topic")


async def paginate(query: Any, page: int, per_page: int = PAGE_SIZE) -> tuple[list[Any], dict[str, Any]]:
    page = max(page, 1)
    total = await query.count()
    pages = max(ceil(total / per_page), 1)
    if page > pages:
        page = pages

    offset = (page - 1) * per_page
    items = await query.offset(offset).limit(per_page)
    return items, {
        "page": page,
        "pages": pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev": page - 1,
        "next": page + 1,
    }


async def log_admin_action(actor: dict[str, Any], title: str, description: str) -> None:
    actor_name = actor.get("username", "Unknown admin")
    actor_id = actor.get("id", "unknown")
    await AdminAction.create(
        title=title,
        description=f"{description}\n\nActor: {actor_name} ({actor_id})",
    )


async def validate_guild_role(request: Request, role_id: str) -> str | None:
    if not role_id.isdigit():
        return "Role ID must contain only numbers."

    rest = request.app.state.discord_rest_app.acquire(secret.TOKEN, "Bot")
    async with rest:
        try:
            await rest.fetch_role(secret.GUILD_ID, role_id)
        except hikari.NotFoundError:
            return "That Discord role does not exist in the configured guild."
        except hikari.ForbiddenError:
            return "Bot does not have permission to validate roles in the configured guild."

    return None


async def read_tier_icon(form: Any) -> tuple[bytes | None, str | None]:
    upload = form.get("icon")
    if upload is None or not getattr(upload, "filename", ""):
        return None, None

    data = await upload.read()
    if not data:
        return None, None

    try:
        image = Image.open(BytesIO(data))
        image.verify()
    except (UnidentifiedImageError, OSError):
        return None, "Tier icon must be a valid image."

    width, height = image.size
    if width != height:
        return None, "Tier icon must be square."
    if width > 512 or height > 512:
        return None, "Tier icon must be 512x512 or smaller."

    return data, None


async def refresh_member_nickname(request: Request, user: User) -> str | None:
    rest = request.app.state.discord_rest_app.acquire(secret.TOKEN, "Bot")
    async with rest:
        try:
            member = await rest.fetch_member(secret.GUILD_ID, user.discord_id)
        except hikari.NotFoundError:
            return "Discord member was not found."

        current_name = member.global_name or member.username
        nickname = await get_match_score_nickname(current_name, user)
        try:
            await rest.edit_member(secret.GUILD_ID, user.discord_id, nickname=nickname)
        except hikari.ForbiddenError:
            return "Bot does not have permission to update one or more nicknames."

    return None


async def refresh_member_tier_roles(request: Request, user: User) -> str | None:
    rest = request.app.state.discord_rest_app.acquire(secret.TOKEN, "Bot")
    async with rest:
        return await sync_tier_roles(user, rest=rest, guild_id=secret.GUILD_ID)


async def user_has_admin_role(request: Request, user_id: str) -> bool:
    if not secret.GUILD_ID or not secret.ADMIN_ROLE_ID:
        return False

    rest = request.app.state.discord_rest_app.acquire(secret.TOKEN, "Bot")
    async with rest:
        try:
            member = await rest.fetch_member(secret.GUILD_ID, user_id)
        except hikari.NotFoundError:
            return False

    return str(secret.ADMIN_ROLE_ID) in {str(role_id) for role_id in member.role_ids}


def avatar_url(user: dict[str, Any]) -> str | None:
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png"

    return None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_logged_in_user(request)
    if user is not None:
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "login_url": "/login",
            "error": request.query_params.get("error"),
        },
    )


@app.get("/login")
async def login():
    if not secret.DISCORD_OAUTH_REDIRECT_URI:
        return RedirectResponse(
            "/?error=missing_oauth_config",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(discord.oauth_login_url)


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None):
    if not code:
        return RedirectResponse(
            "/?error=missing_code",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    access_token, _refresh_token = await discord.get_access_token(code)
    if not access_token:
        return RedirectResponse(
            "/?error=oauth_failed",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        discord_user = await discord.request("/users/@me", access_token)
    except Unauthorized:
        return RedirectResponse(
            "/?error=oauth_failed",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not discord_user.get("id"):
        return RedirectResponse(
            "/?error=oauth_failed",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not await user_has_admin_role(request, discord_user["id"]):
        return RedirectResponse(
            "/?error=not_admin",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    session_user = {
        "id": discord_user["id"],
        "username": discord_user.get("username", "Discord user"),
        "discriminator": discord_user.get("discriminator"),
        "avatar_url": avatar_url(discord_user),
    }
    request.session["user"] = session_user
    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    user: dict[str, Any] = Depends(require_admin_user),
):
    counts = {
        "tiers": await Tier.all().count(),
        "users": await User.all().count(),
        "incomplete_matches": await IncompleteMatch.all().count(),
        "matches": await Match.all().count(),
        "admin_actions": await AdminAction.all().count(),
    }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "counts": counts,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/tiers", response_class=HTMLResponse)
async def tiers_index(request: Request, page: int = 1, user: dict[str, Any] = Depends(require_admin_user)):
    tiers, pagination = await paginate(Tier.all().order_by("elo_min", "elo_max"), page)
    return templates.TemplateResponse(
        request,
        "tiers.html",
        {
            "user": user,
            "tiers": tiers,
            "pagination": pagination,
            "base_path": "/admin/tiers",
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/tiers/new", response_class=HTMLResponse)
async def tier_new(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request,
        "tier_form.html",
        {"user": user, "tier": None, "error": request.query_params.get("error")},
    )


@app.get("/admin/tiers/{tier_id}/edit", response_class=HTMLResponse)
async def tier_edit(request: Request, tier_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    tier = await Tier.get(id=tier_id)
    return templates.TemplateResponse(
        request,
        "tier_form.html",
        {"user": user, "tier": tier, "error": request.query_params.get("error")},
    )


@app.post("/admin/tiers/save")
async def tier_save(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    form = await request.form()
    tier_id = none_if_empty(form.get("id"))
    role_id = str(form.get("role_id", "")).strip()
    role_error = await validate_guild_role(request, role_id)
    if role_error:
        path = f"/admin/tiers/{tier_id}/edit" if tier_id else "/admin/tiers/new"
        return admin_redirect(path, error=role_error)

    icon_data, icon_error = await read_tier_icon(form)
    if icon_error:
        path = f"/admin/tiers/{tier_id}/edit" if tier_id else "/admin/tiers/new"
        return admin_redirect(path, error=icon_error)

    try:
        async with in_transaction():
            tier = await Tier.get(id=int(tier_id)) if tier_id else Tier()
            tier.name = str(form.get("name", "")).strip()
            tier.elo_min = int(form.get("elo_min", 0))
            tier.elo_max = int(form.get("elo_max", 0))
            tier.role_id = role_id
            tier.color = str(form.get("color", "#6366f1")).strip()
            tier.k_factor = int(form.get("k_factor", 0))
            if icon_data is not None:
                tier.icon = icon_data
            elif form_bool(form, "clear_icon"):
                tier.icon = None

            await tier.save()
            await log_admin_action(
                user,
                "Tier saved",
                f"Saved tier '{tier.name}' with Elo range {tier.elo_min}-{tier.elo_max}.",
            )
    except (IntegrityError, ValidationError, ValueError) as exc:
        path = f"/admin/tiers/{tier_id}/edit" if tier_id else "/admin/tiers/new"
        return admin_redirect(path, error=str(exc))

    return admin_redirect("/admin/tiers", notice="saved")


@app.post("/admin/tiers/{tier_id}/delete")
async def tier_delete(tier_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    try:
        async with in_transaction():
            tier = await Tier.get(id=tier_id)
            tier_name = tier.name
            await tier.delete()
            await log_admin_action(user, "Tier deleted", f"Deleted tier '{tier_name}'.")
    except (DoesNotExist, IntegrityError) as exc:
        return admin_redirect("/admin/tiers", error=str(exc))

    return admin_redirect("/admin/tiers", notice="deleted")


@app.get("/admin/tiers/{tier_id}/icon")
async def tier_icon(tier_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    tier = await Tier.get(id=tier_id)
    if not tier.icon:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return Response(bytes(tier.icon), media_type="image/png")


@app.get("/admin/users", response_class=HTMLResponse)
async def users_index(request: Request, page: int = 1, user: dict[str, Any] = Depends(require_admin_user)):
    users, pagination = await paginate(User.all().prefetch_related("tier").order_by("-elo", "discord_name"), page)
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "user": user,
            "users": users,
            "pagination": pagination,
            "base_path": "/admin/users",
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/users/new", response_class=HTMLResponse)
async def user_new(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {"user": user, "bot_user": None, "tiers": await tier_choices(), "error": request.query_params.get("error")},
    )


@app.get("/admin/users/{discord_id}/edit", response_class=HTMLResponse)
async def user_edit(request: Request, discord_id: str, user: dict[str, Any] = Depends(require_admin_user)):
    bot_user = await User.get(discord_id=discord_id).prefetch_related("tier")
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {"user": user, "bot_user": bot_user, "tiers": await tier_choices(), "error": request.query_params.get("error")},
    )


@app.post("/admin/users/save")
async def user_save(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    form = await request.form()
    original_id = none_if_empty(form.get("original_discord_id"))
    discord_id = str(form.get("discord_id", "")).strip()
    if not discord_id.isdigit():
        path = f"/admin/users/{original_id}/edit" if original_id else "/admin/users/new"
        return admin_redirect(path, error="Discord ID must contain only numbers.")

    try:
        async with in_transaction():
            bot_user = await User.get(discord_id=original_id) if original_id else User(discord_id=discord_id)
            bot_user.discord_id = discord_id
            bot_user.discord_name = str(form.get("discord_name", "")).strip()
            bot_user.elo = max(0, int(form.get("elo", 0)))
            bot_user.tier_id = int(form.get("tier_id"))
            await bot_user.save()
            await log_admin_action(user, "User saved", f"Saved user '{bot_user.discord_name}' ({bot_user.discord_id}).")
    except (IntegrityError, ValidationError, ValueError) as exc:
        path = f"/admin/users/{original_id}/edit" if original_id else "/admin/users/new"
        return admin_redirect(path, error=str(exc))

    bot_user = await User.get(discord_id=discord_id)
    bot_user.tier = await bot_user.calculate_dynamic_tier_object()
    await bot_user.save()
    role_error = await refresh_member_tier_roles(request, bot_user)
    if role_error:
        return admin_redirect("/admin/users", error=role_error)

    return admin_redirect("/admin/users", notice="saved")


@app.post("/admin/users/{discord_id}/delete")
async def user_delete(discord_id: str, user: dict[str, Any] = Depends(require_admin_user)):
    try:
        async with in_transaction():
            bot_user = await User.get(discord_id=discord_id)
            user_name = bot_user.discord_name
            await bot_user.delete()
            await log_admin_action(user, "User deleted", f"Deleted user '{user_name}' ({discord_id}).")
    except (DoesNotExist, IntegrityError) as exc:
        return admin_redirect("/admin/users", error=str(exc))

    return admin_redirect("/admin/users", notice="deleted")


@app.post("/admin/users/{discord_id}/refresh-nickname")
async def user_refresh_nickname(
    request: Request,
    discord_id: str,
    user: dict[str, Any] = Depends(require_admin_user),
):
    bot_user = await User.get(discord_id=discord_id)
    error = await refresh_member_nickname(request, bot_user)
    if error:
        return admin_redirect("/admin/users", error=error)

    await log_admin_action(user, "Nickname refreshed", f"Refreshed nickname for '{bot_user.discord_name}' ({discord_id}).")
    return admin_redirect("/admin/users", notice="nickname_updated")


@app.get("/admin/incomplete-matches", response_class=HTMLResponse)
async def incomplete_matches_index(request: Request, page: int = 1, user: dict[str, Any] = Depends(require_admin_user)):
    matches, pagination = await paginate(
        IncompleteMatch.all().prefetch_related("asker", "opposer", "judge").order_by("-ongoing", "-started_at", "topic"),
        page,
    )
    return templates.TemplateResponse(
        request,
        "incomplete_matches.html",
        {
            "user": user,
            "matches": matches,
            "pagination": pagination,
            "base_path": "/admin/incomplete-matches",
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/incomplete-matches/new", response_class=HTMLResponse)
async def incomplete_match_new(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    return admin_redirect("/admin/incomplete-matches", error="Create matches with the Discord bot.")


@app.get("/admin/incomplete-matches/{match_id}/edit", response_class=HTMLResponse)
async def incomplete_match_edit(request: Request, match_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    match = await IncompleteMatch.get(id=match_id).prefetch_related("asker", "opposer", "judge")
    return templates.TemplateResponse(
        request,
        "incomplete_match_form.html",
        {
            "user": user,
            "match": match,
            "users": await user_choices(),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/admin/incomplete-matches/save")
async def incomplete_match_save(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    form = await request.form()
    match_id = none_if_empty(form.get("id"))
    if not match_id:
        return admin_redirect("/admin/incomplete-matches", error="Create matches with the Discord bot.")

    match = await IncompleteMatch.get(id=int(match_id))
    match.asker_id = str(form.get("asker_id", "")).strip()
    match.opposer_id = none_if_empty(form.get("opposer_id"))
    match.judge_id = none_if_empty(form.get("judge_id"))
    match.topic = str(form.get("topic", "")).strip()
    match.ongoing = form_bool(form, "ongoing")
    match.started = form_bool(form, "started")
    match.started_at = parse_datetime(form.get("started_at"))
    match.ended_at = parse_datetime(form.get("ended_at"))

    try:
        await match.save()
    except (IntegrityError, ValidationError, ValueError) as exc:
        path = f"/admin/incomplete-matches/{match_id}/edit" if match_id else "/admin/incomplete-matches/new"
        return admin_redirect(path, error=str(exc))

    await log_admin_action(user, "Open match saved", f"Saved open match #{match.id}: '{match.topic}'.")
    return admin_redirect("/admin/incomplete-matches", notice="saved")


@app.post("/admin/incomplete-matches/{match_id}/delete")
async def incomplete_match_delete(match_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    try:
        match = await IncompleteMatch.get(id=match_id)
        topic = match.topic
        await match.delete()
    except (DoesNotExist, IntegrityError) as exc:
        return admin_redirect("/admin/incomplete-matches", error=str(exc))

    await log_admin_action(user, "Open match deleted", f"Deleted open match #{match_id}: '{topic}'.")
    return admin_redirect("/admin/incomplete-matches", notice="deleted")


@app.get("/admin/matches", response_class=HTMLResponse)
async def matches_index(request: Request, page: int = 1, user: dict[str, Any] = Depends(require_admin_user)):
    matches, pagination = await paginate(
        Match.all().prefetch_related("winner", "loser", "judge", "incomplete_match").order_by("-completed_at"),
        page,
    )
    return templates.TemplateResponse(
        request,
        "matches.html",
        {
            "user": user,
            "matches": matches,
            "pagination": pagination,
            "base_path": "/admin/matches",
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/matches/new", response_class=HTMLResponse)
async def match_new(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    return admin_redirect("/admin/matches", error="Create matches with the Discord bot.")


@app.get("/admin/matches/{match_id}/edit", response_class=HTMLResponse)
async def match_edit(request: Request, match_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    match = await Match.get(id=match_id).prefetch_related("winner", "loser", "judge", "incomplete_match")
    return templates.TemplateResponse(
        request,
        "match_form.html",
        {
            "user": user,
            "match": match,
            "users": await user_choices(),
            "incomplete_matches": await incomplete_match_choices(),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/admin/matches/save")
async def match_save(request: Request, user: dict[str, Any] = Depends(require_admin_user)):
    form = await request.form()
    match_id = none_if_empty(form.get("id"))
    if not match_id:
        return admin_redirect("/admin/matches", error="Create matches with the Discord bot.")

    match = await Match.get(id=int(match_id))
    match.winner_id = str(form.get("winner_id", "")).strip()
    match.loser_id = str(form.get("loser_id", "")).strip()
    match.judge_id = str(form.get("judge_id", "")).strip()
    match.topic = str(form.get("topic", "")).strip()
    match.nulled = form_bool(form, "nulled")
    match.draw = form_bool(form, "draw")
    match.stance_description = str(form.get("stance_description", ""))
    match.additional_notes = str(form.get("additional_notes", ""))
    match.incomplete_match_id = int(form.get("incomplete_match_id"))

    try:
        await match.save()
    except (IntegrityError, ValidationError, ValueError) as exc:
        path = f"/admin/matches/{match_id}/edit" if match_id else "/admin/matches/new"
        return admin_redirect(path, error=str(exc))

    await log_admin_action(user, "Match saved", f"Saved completed match #{match.id}: '{match.topic}'.")
    return admin_redirect("/admin/matches", notice="saved")


@app.post("/admin/matches/{match_id}/annul")
async def match_annul(
    request: Request,
    match_id: int,
    user: dict[str, Any] = Depends(require_admin_user),
):
    match = await Match.get(id=match_id).prefetch_related("winner", "loser")
    if match.nulled:
        return admin_redirect("/admin/matches", error="already_nulled")

    try:
        await match.annul()
    except (IntegrityError, ValidationError, ValueError) as exc:
        return admin_redirect("/admin/matches", error=str(exc))

    winner = await User.get(discord_id=match.winner_id)
    loser = await User.get(discord_id=match.loser_id)
    for bot_user in (winner, loser):
        bot_user.tier = await bot_user.calculate_dynamic_tier_object()
        await bot_user.save()
        error = await refresh_member_nickname(request, bot_user)
        if error:
            return admin_redirect("/admin/matches", error=error)
        role_error = await refresh_member_tier_roles(request, bot_user)
        if role_error:
            return admin_redirect("/admin/matches", error=role_error)

    await log_admin_action(user, "Match annulled", f"Annulled match #{match.id}: '{match.topic}'.")
    return admin_redirect("/admin/matches", notice="annulled")


@app.post("/admin/matches/{match_id}/delete")
async def match_delete(match_id: int, user: dict[str, Any] = Depends(require_admin_user)):
    try:
        match = await Match.get(id=match_id)
        topic = match.topic
        await match.delete()
    except (DoesNotExist, IntegrityError) as exc:
        return admin_redirect("/admin/matches", error=str(exc))

    await log_admin_action(user, "Match deleted", f"Deleted completed match #{match_id}: '{topic}'.")
    return admin_redirect("/admin/matches", notice="deleted")


@app.get("/admin/actions", response_class=HTMLResponse)
async def admin_actions_index(request: Request, page: int = 1, user: dict[str, Any] = Depends(require_admin_user)):
    actions, pagination = await paginate(AdminAction.all().order_by("-created_at"), page)
    return templates.TemplateResponse(
        request,
        "admin_actions.html",
        {
            "user": user,
            "actions": actions,
            "pagination": pagination,
            "base_path": "/admin/actions",
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return response
