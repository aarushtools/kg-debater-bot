import time
from datetime import datetime, timezone

import hikari
import lightbulb
import miru
from aiocache import Cache
from tortoise.exceptions import DoesNotExist, MultipleObjectsReturned
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
        for user_str, request_user in [("opposer", self.opposing_user), ("judge", self.judge)]:
            request_text = ("Debate Request", "Debate request") if user_str == "opposer" else ("Judge Request", "Judge request")

            view = LimitedAcceptDenyView(asker=ctx.member, user_requested=request_user, user_request_str=user_str)
            embed = hikari.Embed(title=request_text[0], color=0xFF0000)
            embed.add_field(name="Asker", value=ctx.member.mention)
            embed.add_field(name="Opposer", value=request_user.mention)
            embed.add_field(name="Judge", value=self.judge.mention)
            embed.add_field(name="Topic", value=self.topic)

            response_id = await ctx.respond(content=f"{request_text[1]} for: {request_user.mention}", embed=embed, components=view)
            miru_client.start_view(view)

            await view.wait()

            if view.answer is None:
                await ctx.edit_response(response_id=response_id,
                                        content=f"{request_user.mention} did not respond for a {user_str} request within 120 seconds.")
                incomplete_match_obj.ongoing = False
                await incomplete_match_obj.save()
                return
            elif view.answer is False:
                await ctx.edit_response(response_id=response_id,
                                        content=f"{request_user.mention} denied this {user_str} request.")
                incomplete_match_obj.ongoing = False
                await incomplete_match_obj.save()
                return
            elif view.answer == "cancel":
                await ctx.edit_response(response_id=response_id,
                                        content=f"{ctx.member.mention} canceled this {user_str} request.")
                incomplete_match_obj.ongoing = False
                await incomplete_match_obj.save()
                return
            elif view.answer is True:
                await ctx.edit_response(response_id=response_id,
                                        content=f"{request_user.mention} accepted this {user_str} request.")

        # Here, opposer and judge has accepted
        incomplete_match_obj.ongoing = True
        incomplete_match_obj.started = True

        incomplete_match_obj.started_at = datetime.now(timezone.utc)
        await incomplete_match_obj.save()


        embed = hikari.Embed(title="Match Started", color=0xFF0000)
        embed.set_image()  # TODO
        embed.add_field(name="Topic", value=self.topic)
        embed.add_field(name="Start Time", value=f"<t:{int(incomplete_match_obj.started_at.timestamp())}:F>")

        await ctx.respond(embed=embed)

@debate.register
class CancelDebate(lightbulb.SlashCommand, name="cancel", description="Request to cancel an ongoing debate you are participating in"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        model_user = User.get(discord_id=ctx.member.id)
        try:
            incomplete_match_obj = await IncompleteMatch.get(Q(ongoing=True) & Q(started=True) & (Q(asker=model_user) | Q(opposer=model_user)))
        except MultipleObjectsReturned:
            await ctx.respond("You are currently in multiple incomplete matches. If you think this is a bug, ask an admin to cancel them for you.", ephemeral=True)
            return
        except DoesNotExist:
            await ctx.respond("You are not in any ongoing matches currently", ephemeral=True)
            return

        model_request_user = incomplete_match_obj.opposer
        request_user = bot.cache.get_member(ctx.guild_id, model_request_user.discord_id) or await bot.rest.fetch_member(ctx.guild_id, model_request_user.discord_id)

        view = LimitedAcceptDenyView(asker=ctx.member, user_requested=request_user, user_request_str="opposer")
        embed = hikari.Embed(title="Cancel Match", description=f"{ctx.member.mention} requested to cancel a debate with {request_user.mention}",color=0xFF0000)

        response_id = await ctx.respond(content=f"{ctx.member.mention} wants to cancel an ongoing debate. Request for: {request_user.mention}", embed=embed,
                                        components=view)
        miru_client.start_view(view)

        await view.wait()

        if view.answer is None:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{request_user.mention} did not respond for a cancel debate request within 120 seconds.")
            return
        elif view.answer is False:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{request_user.mention} denied this cancal debate request.")
            return
        elif view.answer == "cancel":
            await ctx.edit_response(response_id=response_id,
                                    content=f"{ctx.member.mention} canceled this cancel debate request.")
            return
        elif view.answer is True:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{request_user.mention} accepted this cancel debate request.")
            incomplete_match_obj.ongoing = False
            await incomplete_match_obj.save()
            await Match.create(winner=model_user, loser=model_request_user, judge=incomplete_match_obj.judge, topic=incomplete_match_obj.topic, nulled=True, draw=True)
            return

        await ctx.respond("Debate canceled.")

class LimitedAcceptDenyView(miru.View):
    def __init__(self, asker: hikari.User, user_requested: hikari.User, user_request_str: str, *args, **kwargs):
        self.asker = asker
        self.user_requested = user_requested
        self.user_request_str = user_request_str
        self.answer = None
        super().__init__(timeout=120, *args, **kwargs)

    @miru.button(label="Accept", style=hikari.ButtonStyle.SUCCESS)
    async def accept(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.user_requested.id:
            await ctx.respond(f"Only the requested {self.user_request_str} can accept!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = True
        await self.disable_ui()

    @miru.button(label="Deny", style=hikari.ButtonStyle.DANGER)
    async def deny(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.user_requested.id:
            await ctx.respond(f"Only the requested {self.user_request_str} can deny!", flags=hikari.MessageFlag.EPHEMERAL)
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
