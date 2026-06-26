from pathlib import Path

import aiohttp
import hikari
import sys

from tortoise import Tortoise

from helpers import get_match_score_nickname, sync_tier_roles

args = sys.argv[1:]
import secret
from models import User, Tier, start_db

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL,)

async def init_users_table(_: hikari.StartedEvent):
    await bot.request_guild_members(int(secret.GUILD_ID))

@bot.listen(hikari.MemberChunkEvent)
async def on_member_chunk(event: hikari.MemberChunkEvent) -> None:
    created_count = 0
    already_existed = 0
    default_tier = await Tier.objects.get_default_tier()

    for member_id, member in event.members.items():
        if member.is_bot:
            continue

        model_user, created = await User.get_or_create(discord_id=member_id, defaults={
            "discord_name": member.username,
            "tier": default_tier,
        })

        match_nick = await get_match_score_nickname(member.global_name or member.username, model_user)
        try:
            if match_nick != member.nickname:
                await member.edit(nickname=match_nick)
        except hikari.ForbiddenError:
            print(f"Couldn't edit {member.display_name}'s nickname")

        role_error = await sync_tier_roles(model_user, member=member)
        if role_error:
            print(f"Couldn't sync tier roles for {member.display_name}: {role_error}")

        if created: created_count += 1
        else: already_existed += 1

        if not Path("images/test1.bytes").exists():
            with open("images/test1.bytes", "wb") as f:
                f.write(await member.display_avatar_url.read())
        elif not Path("images/test2.bytes").exists():
            with open("images/test2.bytes", "wb") as f:
                f.write(await member.display_avatar_url.read())

    print(f"Created {created_count} new users. {already_existed} already existed in the database. Total: {created_count + already_existed}")

async def drop_and_reset(event: hikari.StartedEvent):
    await Tortoise._drop_databases()
    await Tortoise.init(config=secret.TORTOISE_ORM, _enable_global_fallback=False)
    await Tortoise.generate_schemas(safe=True)
    await Tier.create(name="Low Tier", elo_min=0, elo_max=0, role_id="021290824", color="#FF7485", k_factor=10)

if __name__ == "__main__":
    if "init_db" in args:
        bot.subscribe(hikari.StartedEvent, init_users_table)
    if "drop_and_reset" in args:
        bot.subscribe(hikari.StartedEvent, drop_and_reset)

    bot.subscribe(hikari.StartingEvent, start_db)
    bot.run()