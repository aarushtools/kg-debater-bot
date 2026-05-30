import asyncio

import hikari
import lightbulb
import miru
from aiocache import Cache
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import Q

import secret
from helpers import get_match_score_nickname
from models import User, Tier, Match, IncompleteMatch, start_db

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL)
lb_client = lightbulb.client_from_app(bot)
miru_client = miru.Client(bot)
cache = Cache(Cache.MEMORY)

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

@lb_client.register
class Stats(lightbulb.SlashCommand, name="stats", description="View debate stats globally"):
    user = lightbulb.user("user", "The user you want to view stats for", default=None)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, profile_user: hikari.User | None = None) -> None:
        single_user = self.user if self.user else profile_user
        users = await User.all().order_by("-elo")[:10] if not single_user else await User.filter(discord_id=single_user.id)  # This only returns 1 object on purpose so we can iterate regardless
        stats_dict = {}
        for user in users:
            matches_won, matches_lost = await user.get_match_score()
            matches_judged = await user.matches_judged.all().count()
            stats_dict[user] = {
                "elo": user.elo,
                "matches_won": matches_won,
                "matches_lost": matches_lost,
                "matches_judged": matches_judged,
                "matches_played": matches_won + matches_lost,
            }

@lb_client.register
class GetUserStats(lightbulb.UserCommand, name="View debate stats"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await Stats.invoke(ctx, self.target)  # todo change this

debate = lightbulb.Group("debate", "View debate commands")

@debate.register
class StartDebate(lightbulb.SlashCommand, name="start", description="Start a debate"):
    opposing_user = lightbulb.user("user", "The user you want to debate against")
    judge = lightbulb.user("judge", "The user you want to judge the debate")
    topic = lightbulb.string("topic", "The topic you want to debate about")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        if self.opposing_user.id == ctx.member.id:
            await ctx.respond("You can't debate yourself!", ephemeral=True)
            return
        #if self.judge.id in (self.opposing_user.id, ctx.member.id):
        #    await ctx.respond("One of the debate participants is a judge! Refusing this request.", ephemeral=True)
        #    return

        if len(self.topic) > 255:
            await ctx.respond("Topic too long (exceeds 255 characters)", ephemeral=True)
            return

        try:
            model_opposer = await User.get(discord_id=self.opposing_user.id)
            model_asker = await User.get(discord_id=ctx.member.id)
            model_judge = await User.get(discord_id=self.judge.id)
        except DoesNotExist as e:
            await ctx.respond(f"A user doesn't exist in the database.", ephemeral=True)
            return

        # Ensure opposing user nor command user are currently in a debate
        incomplete_matches_opposer = await IncompleteMatch.filter(Q(ongoing=True) & (
                Q(asker=model_opposer) | Q(opposer=model_opposer)
        )).prefetch_related("asker", "opposer", "judge")

        incomplete_matches_asker = await IncompleteMatch.filter(Q(ongoing=True) & (
            Q(asker=model_asker) | Q(opposer=model_asker)
        )).prefetch_related("asker", "opposer", "judge")

        if incomplete_matches_opposer:
            await ctx.respond(f"Your proposed opponent  {self.opposing_user.mention} is already in a debate!"
                              f" Specifically:\n {'\n'.join(f'> {m}' for m in incomplete_matches_opposer)}", ephemeral=True)
            return

        if incomplete_matches_asker:
            await ctx.respond(f"You are already in a debate!"
                              f" Specifically:\n {'\n'.join(f'> {m}' for m in incomplete_matches_asker)}. If you think this is a mistake, ask an admin"
                              f" to cancel your current ongoing debates.", ephemeral=True)
            return

        incomplete_match_obj = await IncompleteMatch.create(asker=model_asker, opposer=model_opposer, judge=model_judge, topic=self.topic)

        # Ask the opposing user to confirm
        view = LimitedAcceptDenyView(asker=ctx.member, opposer=self.opposing_user)
        embed = hikari.Embed(title="Debate Request", color=0xFF0000)
        embed.add_field(name="Asker", value=ctx.member.mention)
        embed.add_field(name="Opposer", value=self.opposing_user.mention)
        embed.add_field(name="Judge", value=self.judge.mention)
        embed.add_field(name="Topic", value=self.topic)

        response_id = await ctx.respond(content=f"Debate request for: {self.opposing_user.mention}", embed=embed, components=view)
        miru_client.start_view(view)

        await view.wait()

        if view.answer is None:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{self.opposing_user.mention} did not respond within 120 seconds.")
            incomplete_match_obj.ongoing = False
            await incomplete_match_obj.save()
        elif view.answer is False:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{self.opposing_user.mention} denied this request.")
            incomplete_match_obj.ongoing = False
            await incomplete_match_obj.save()
        elif view.answer == "cancel":
            await ctx.edit_response(response_id=response_id,
                                    content=f"{ctx.member.mention} canceled this request.")
            incomplete_match_obj.ongoing = False
            await incomplete_match_obj.save()
        elif view.answer is True:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{self.opposing_user.mention} accepted this request.")

class LimitedAcceptDenyView(miru.View):
    def __init__(self, asker: hikari.User, opposer: hikari.User, *args, **kwargs):
        self.asker = asker
        self.opposer = opposer
        self.answer = None
        super().__init__(timeout=120, *args, **kwargs)

    @miru.button(label="Accept", style=hikari.ButtonStyle.SUCCESS)
    async def accept(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.opposer.id:
            await ctx.respond("Only the requested opponent can accept!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = True
        await self.disable_ui()

    @miru.button(label="Deny", style=hikari.ButtonStyle.DANGER)
    async def deny(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.opposer.id:
            await ctx.respond("Only the requested opponent can deny!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = False
        await self.disable_ui()


    @miru.button(label="Cancel (for asker)", style=hikari.ButtonStyle.SECONDARY)
    async def cancel(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.asker.id:
            await ctx.respond(f"Only the asker ({self.asker.mention}) can cancel!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = "cancel"
        await self.disable_ui()

    async def on_timeout(self) -> None:
        await self.disable_ui()

    async def disable_ui(self):
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(components=self)
            except hikari.NotFoundError:
                pass

        self.stop()

bot.subscribe(hikari.StartingEvent, start_db)
bot.subscribe(hikari.StartingEvent, lb_client.start)
lb_client.register(debate)
bot.run()
