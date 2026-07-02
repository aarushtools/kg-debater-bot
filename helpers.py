import logging

import hikari

import models
from models import Tier

logger = logging.getLogger(__name__)

_STALE_DISCORD_RESOURCE_CODES = frozenset({
    10003,  # Unknown Channel
    10008,  # Unknown Message
    10015,  # Unknown Webhook
    10062,  # Unknown Interaction
    40060,  # Interaction has already been acknowledged
    50027,  # Invalid Webhook Token
})


def is_stale_discord_resource_error(exc: BaseException) -> bool:
    if isinstance(exc, hikari.NotFoundError):
        return True

    if isinstance(exc, hikari.HTTPError) and exc.code in _STALE_DISCORD_RESOURCE_CODES:
        return True

    return False


async def safe_defer_response(ctx, **kwargs) -> bool:
    try:
        await ctx.defer(**kwargs)
        return True
    except Exception as exc:
        if is_stale_discord_resource_error(exc):
            logger.warning("Skipping defer; interaction is no longer available: %s", exc)
            return False
        raise


async def safe_edit_response(ctx, *, response_id=None, **kwargs) -> bool:
    try:
        if response_id is not None:
            await ctx.edit_response(response_id=response_id, **kwargs)
        else:
            await ctx.edit_response(**kwargs)
        return True
    except Exception as exc:
        if is_stale_discord_resource_error(exc):
            logger.warning("Skipping edit_response; interaction/message is no longer available: %s", exc)
            return False
        raise


async def get_tier_role_ids() -> set[int]:
    role_ids = await Tier.all().values_list("role_id", flat=True)
    return {int(role_id) for role_id in role_ids if str(role_id).isdigit()}


async def sync_tier_roles(
    user: models.User,
    *,
    member: hikari.Member | None = None,
    rest: hikari.api.RESTClient | None = None,
    guild_id: int | str | None = None,
) -> str | None:
    tier = await user.calculate_dynamic_tier_object()
    if not str(tier.role_id).isdigit():
        return None

    target_role_id = int(tier.role_id)
    tier_role_ids = await get_tier_role_ids()

    if member is None:
        if rest is None or guild_id is None:
            return "Missing Discord client for tier role sync."
        try:
            member = await rest.fetch_member(int(guild_id), int(user.discord_id))
        except hikari.NotFoundError:
            return "Discord member was not found."

    role_ids = set(member.role_ids)
    to_remove = [role_id for role_id in role_ids if role_id in tier_role_ids and role_id != target_role_id]
    needs_add = target_role_id not in role_ids

    if not to_remove and not needs_add:
        return None

    try:
        if rest is not None and guild_id is not None:
            guild = int(guild_id)
            discord_user_id = int(user.discord_id)
            for role_id in to_remove:
                await rest.remove_role_from_member(guild, discord_user_id, role_id)
            if needs_add:
                await rest.add_role_to_member(guild, discord_user_id, target_role_id)
        else:
            for role_id in to_remove:
                await member.remove_role(role_id)
            if needs_add:
                await member.add_role(target_role_id)
    except hikari.ForbiddenError:
        return "Bot does not have permission to update tier roles."
    except hikari.NotFoundError:
        return "Discord member or role was not found."

    return None


async def get_match_score_nickname(current_name: str, user: models.User):
    wins, losses = await user.get_match_score()
    record_suffix = f"({wins}-{losses})"
    if len(current_name) + len(record_suffix) > 31:
        # Prevent nickname with record from exceeding discord nickname character limit
        current_name = current_name[:31 - len(record_suffix)]

    return f"{current_name} {record_suffix}"