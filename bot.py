import asyncio

import hikari
import lightbulb
from tortoise import Tortoise

import secret
from helpers import get_match_score_nickname
from models import User, Tier

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL)
client = lightbulb.client_from_app(bot)

@bot.listen(hikari.MemberCreateEvent)
async def on_member_join(event: hikari.MemberCreateEvent) -> None:
    member = event.member
    current_name = member.global_name or member.username

    # Initialize user in database (if not already)
    model_user, _ = await User.get_or_create(discord_id=member.id, defaults={
        "discord_name": member.username,
        "tier": await Tier.objects.get_default_tier()
    })

    await member.edit(nickname=await get_match_score_nickname(current_name, model_user))

@client.register
class Stats(lightbulb.SlashCommand, name="stats", description="View debate stats globally"):
    user = lightbulb.user("user", "The user you want to view stats for", default=None)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, profile_user: hikari.User | None = None) -> None:
        single_user = self.user if self.user else profile_user
        users = await User.all().order_by("-elo")[:10] if not single_user else await User.filter(discord_id=single_user.id)  # This only returns 1 object on purpose so we can iterate regardless
        stats_dict = {}
        for user in users:
            matches_won, matches_lost = await user.get_match_score()
            matches_judged = await user.matches_judged.count()
            stats_dict[user] = {
                "elo": user.elo,
                "matches_won": matches_won,
                "matches_lost": matches_lost,
                "matches_judged": matches_judged,
                "matches_played": matches_won + matches_lost,
            }

@client.register
class GetUserStats(lightbulb.UserCommand, name="View debate stats"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await Stats.invoke(ctx, self.target)  # todo change this


async def on_startup(event: hikari.StartingEvent) -> None:
    if not Tortoise.is_inited():
        await Tortoise.init(config=secret.TORTOISE_ORM, _enable_global_fallback=True)

bot.subscribe(hikari.StartingEvent, on_startup)
bot.subscribe(hikari.StartingEvent, client.start)
bot.run()
