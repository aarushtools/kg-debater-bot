import hikari
import sys;

from helpers import get_match_score_nickname

args = sys.argv[1:]
import secret
from models import User, Tier, start_db

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL)

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

        if created: created_count += 1
        else: already_existed += 1

    print(f"Created {created_count} new users. {already_existed} already existed in the database. Total: {created_count + already_existed}")


if __name__ == "__main__":
    if "init_db" in args:
        bot.subscribe(hikari.StartedEvent, init_users_table)

    bot.subscribe(hikari.StartingEvent, start_db)
    bot.run()