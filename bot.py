import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import hikari
import lightbulb
import miru
from aiocache import Cache
from easy_pil import Editor, Font
from tortoise.exceptions import DoesNotExist, MultipleObjectsReturned
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

import secret
from helpers import get_match_score_nickname
from models import User, Tier, Match, IncompleteMatch, start_db

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL)
lb_client = lightbulb.client_from_app(bot)
miru_client = miru.Client(bot)
cache = Cache(Cache.MEMORY)

@dataclass
class NonAsyncHikariMember:
    avatar_bytes: bytes
    display_name: str
    user: User
    tier: Tier

@dataclass
class NonAsyncMatch:
    match_obj: Match
    elo_winner_change: int
    elo_loser_change: int
    winner_na: NonAsyncHikariMember | None = None
    loser_na: NonAsyncHikariMember | None = None


class DisableView(miru.View):
    async def disable_and_stop_ui(self):
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(components=self)
            except hikari.NotFoundError:
                pass

        self.stop()

async def build_nonasync_member(member: hikari.Member):
    model_user = await User.get(discord_id=member.id)
    return NonAsyncHikariMember(
        avatar_bytes=await member.display_avatar_url.read(),
        display_name=member.display_name,
        user=model_user,
        tier=await model_user.calculate_dynamic_tier_object()
    )

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
    from_pos = lightbulb.number("page_number", "The position to start the global leaderboard", default=0)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, profile_user: hikari.User | None = None) -> None:
        await ctx.defer(ephemeral=True)

        single_user = self.user or profile_user

        if single_user:
            await send_user_profile(ctx, profile_user)
            return

        view = StatsLeaderboardView(
            start_page=int(self.from_pos),
            guild_id=ctx.guild_id,
        )

        response_id = await ctx.respond(content="Loading...", components=view)
        miru_client.start_view(view)
        await view.update(ctx, response_id=response_id)


async def send_user_profile(ctx: lightbulb.Context, target_user: hikari.User) -> None:
    user = await User.get(discord_id=target_user.id)

    matches_won, matches_lost = await user.get_match_score()
    matches_judged = await user.matches_judged.all().count()

    member = bot.cache.get_member(ctx.guild_id, user.discord_id) or await bot.rest.fetch_member(
        ctx.guild_id, user.discord_id
    )

    stats = {
        "elo": str(user.elo),
        "tier": await user.calculate_dynamic_tier_object(),
        "matches_won": str(matches_won),
        "matches_lost": str(matches_lost),
        "matches_judged": str(matches_judged),
        "matches_participated": str(matches_won + matches_lost + matches_judged),
        "na_member": await build_nonasync_member(member),
        "last_match": await Match.filter(
            Q(winner=user) | Q(loser=user) | Q(judge=user)
        ).order_by("-completed_at").first(),
    }

    embed = hikari.Embed(title=f"{target_user.global_name}'s Profile")
    embed.add_field(name="Elo", value=stats["elo"])
    embed.add_field(name="Tier", value=f"<@&{stats['tier'].role_id}>")
    embed.add_field(name="Matches Won", value=stats["matches_won"])
    embed.add_field(name="Matches Lost", value=stats["matches_lost"])
    embed.add_field(name="Matches Judged", value=stats["matches_judged"])
    embed.add_field(name="Matches Participated", value=stats["matches_participated"])

    if stats["last_match"]:
        embed.add_field(name="Most recent match", value=str(stats["last_match"]))
        embed.add_field(
            name="Last active match",
            value=f"<t:{int(stats['last_match'].completed_at.timestamp())}:F>",
        )

    await ctx.respond(embed=embed, ephemeral=True)

class StatsLeaderboardView(DisableView):
    def __init__(self, start_page: int = 0, guild_id: int | None = None):
        super().__init__(timeout=180)
        self.page = start_page
        self.page_size = 8
        self.guild_id = guild_id
        self._lock = asyncio.Lock()

    def _get_button(self, label: str):
        return next(
            item for item in self.children
            if isinstance(item, miru.Button) and item.label == label
        )

    async def fetch_page(self):
        total = await User.all().count()
        users = await User.all().order_by("-elo")[
                      self.page * self.page_size: self.page * self.page_size + self.page_size
                      ]
        return users, total

    async def build_stats(self, users):
        stats_dict = {}

        for user in users:
            matches_won, matches_lost = await user.get_match_score()
            matches_judged = await user.matches_judged.all().count()

            member = (
                    bot.cache.get_member(self.guild_id, user.discord_id)
                    or await bot.rest.fetch_member(self.guild_id, user.discord_id)
            )

            stats_dict[user.discord_id] = {
                "na_member": await build_nonasync_member(member),
                "elo": str(user.elo),
                "tier": await user.calculate_dynamic_tier_object(),
                "matches_won": str(matches_won),
                "matches_lost": str(matches_lost),
                "matches_judged": str(matches_judged),
                "matches_participated": str(matches_won + matches_lost + matches_judged),
                "last_match": await Match.filter(
                    Q(winner=user) | Q(loser=user) | Q(judge=user)
                ).order_by("-completed_at").first(),
            }

        return stats_dict

    async def render(self):
        users, total = await self.fetch_page()
        stats = await self.build_stats(users)

        image = await asyncio.to_thread(
            generate_lb_image,
            stats,
            self.page,
            self.page == 0,
        )

        embed = hikari.Embed(
            title=f"Leaderboard (Page {self.page + 1})",
            description=f"Showing {self.page * 8 + 1}-{self.page * 8 + len(users)} / {total}",
        )

        prev_button = self._get_button("◀ Prev")
        next_button = self._get_button("Next ▶")
        prev_button.disabled = self.page == 0
        next_button.disabled = (self.page + 1) * self.page_size >= total

        return embed, image

    async def update(self, ctx, response_id=None):
        async with self._lock:
            kwargs = dict(
                content="Loading...",
                components=self,
            )

            if response_id is None:
                await ctx.edit_response(**kwargs)
            else:
                await ctx.edit_response(response_id=response_id, **kwargs)

            await asyncio.sleep(0.2)

            embed, image = await self.render()
            kwargs = dict(
                content=None,
                embed=embed,
                attachment=hikari.Bytes(image, "leaderboard.png"),
                components=self,
            )

            if response_id is None:
                await ctx.edit_response(**kwargs)
            else:
                await ctx.edit_response(response_id=response_id, **kwargs)

    @miru.button(label="◀ Prev", style=hikari.ButtonStyle.SECONDARY)
    async def prev(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()

        async with self._lock:
            if self.page > 0:
                self.page -= 1
            await self.update(ctx)

    @miru.button(label="Next ▶", style=hikari.ButtonStyle.SECONDARY)
    async def next(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()

        async with self._lock:
            self.page += 1
            await self.update(ctx)

    @miru.button(label="⏹ Stop", style=hikari.ButtonStyle.DANGER)
    async def stop_btn(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()
        await self.disable_and_stop_ui()

    async def on_timeout(self):
        await self.disable_and_stop_ui()

@lb_client.register
class GetUserStats(lightbulb.UserCommand, name="View debate stats"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await send_user_profile(ctx, self.target)

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
                                        content=f"{request_user.mention} did not respond for a {user_str} request within 120 seconds.", components=view)
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

        embed = hikari.Embed(title=f"{secret.ONGOING_DEBATE_EMOJI} Match Started", color=0xFF0000)
        attachment = hikari.Bytes(await asyncio.to_thread(
            generate_debate_image,
            "match_started.png",
            await build_nonasync_member(ctx.member),
            await build_nonasync_member(self.opposing_user),
            await build_nonasync_member(self.judge),
        ), "output.png")
        embed.add_field(name="Topic", value=self.topic)
        embed.add_field(name="Start Time", value=f"<t:{int(incomplete_match_obj.started_at.timestamp())}:F>")

        await ctx.respond(attachment=attachment, embed=embed)

@debate.register
class CancelDebate(lightbulb.SlashCommand, name="cancel", description="Request to cancel an ongoing debate you are participating in"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        model_user = await User.get(discord_id=ctx.member.id)
        try:
            incomplete_match_obj = await IncompleteMatch.get(Q(ongoing=True) & Q(started=True) & (Q(asker=model_user) | Q(opposer=model_user))).prefetch_related("asker", "opposer", "judge")
        except MultipleObjectsReturned:
            await ctx.respond("You are currently in multiple incomplete matches. If you think this is a bug, ask an admin to cancel them for you.", ephemeral=True)
            return
        except DoesNotExist:
            await ctx.respond("You are not in any ongoing matches currently", ephemeral=True)
            return

        model_request_user = incomplete_match_obj.opposer
        request_user = bot.cache.get_member(ctx.guild_id, model_request_user.discord_id) or await bot.rest.fetch_member(ctx.guild_id, model_request_user.discord_id)

        view = LimitedAcceptDenyView(asker=ctx.member, user_requested=request_user, user_request_str="opposer")
        embed = hikari.Embed(title="Cancel Match", description=f"{ctx.member.mention} requested to cancel a debate with {request_user.mention}."
                                                               f" The match will be annulled with no elo loss or gain for either side.",color=0xFF0000)
        embed.add_field("Progress", "1/2")

        response_id = await ctx.respond(content=f"{ctx.member.mention} wants to cancel an ongoing debate. Request for: {request_user.mention}", embed=embed,
                                        components=view)
        miru_client.start_view(view)

        await view.wait()

        if view.answer is None:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{request_user.mention} did not respond for a cancel debate request within 120 seconds.", components=view)  # We have to do this or else the buttons don't disable since on timeout no one clicked any button
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
            embed.edit_field(0, "Progress", "2/2")
            await ctx.edit_response(response_id=response_id,
                                    content=f"{request_user.mention} accepted this cancel debate request.", embed=embed)
            async with in_transaction():
                incomplete_match_obj.ongoing = False
                incomplete_match_obj.ended_at = datetime.now(timezone.utc)
                await incomplete_match_obj.save()
                await Match.create(winner=model_user, loser=model_request_user, judge=incomplete_match_obj.judge,
                                   topic=incomplete_match_obj.topic, nulled=True, draw=True,
                                   incomplete_match=incomplete_match_obj)

            return

        await ctx.respond("Debate canceled.")


@debate.register
class FinishDebate(lightbulb.SlashCommand, name="finish", description="Finish a debate (judge only)"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        model_user = await User.get(discord_id=ctx.member.id)
        try:
            incomplete_match_obj = await IncompleteMatch.get(
                Q(ongoing=True) & Q(started=True) & Q(judge=model_user)).prefetch_related(
                "asker", "opposer", "judge")
        except MultipleObjectsReturned:
            await ctx.respond(
                "You are currently in multiple incomplete matches as a judge. If you think this is a bug, ask an admin to cancel them for you.",
                ephemeral=True)
            return
        except DoesNotExist:
            await ctx.respond("You are not a judge in any ongoing matches currently", ephemeral=True)
            return

        asker_user = bot.cache.get_member(ctx.guild_id, incomplete_match_obj.asker.discord_id) or await bot.rest.fetch_member(
            ctx.guild_id, incomplete_match_obj.asker.discord_id)

        opposer_user = bot.cache.get_member(ctx.guild_id, incomplete_match_obj.opposer.discord_id) or await bot.rest.fetch_member(
            ctx.guild_id, incomplete_match_obj.opposer.discord_id)

        embed = hikari.Embed(title="Finish Debate Request", description=f"Judge ({ctx.member.username}), click the button below to end the debate.", color=0xFF0000)
        embed.add_field(name="Asker", value=asker_user.mention)
        embed.add_field(name="Opposer", value=opposer_user.mention)
        embed.add_field(name="Judge", value=ctx.member.mention)
        embed.add_field(name="Topic", value=incomplete_match_obj.topic)

        view = JudgeDebateInputModalView(judge=ctx.member, user_1=asker_user, user_2=opposer_user)

        response_id = await ctx.respond(embed=embed, components=view)

        miru_client.start_view(view)
        await view.wait()

        if not view.stance_description:
            await ctx.edit_response(response_id=response_id,
                                    content=f"{ctx.member.mention} did not respond for a debate finish request within 120 seconds.",
                                    components=view)
            return
        else:
            await ctx.edit_response(response_id=response_id,
                                    components=view)


        async with in_transaction():
            model_winner = await User.get(discord_id=view.winner_id).prefetch_related("tier")
            model_loser = incomplete_match_obj.opposer if incomplete_match_obj.opposer != model_winner else incomplete_match_obj.asker
            old_tier = model_winner.tier
            match_obj = await Match.create(winner=model_winner, loser=model_loser, judge=incomplete_match_obj.judge,
                               topic=incomplete_match_obj.topic, stance_description=view.stance_description, additional_notes=view.additional_notes,
                                           incomplete_match=incomplete_match_obj)

            incomplete_match_obj.ended_at = datetime.now(timezone.utc)
            incomplete_match_obj.ongoing = False
            await incomplete_match_obj.save()

            winner_elo_change, loser_elo_change = await match_obj.apply_elo_changes()
            model_winner = await User.get(discord_id=model_winner.discord_id).prefetch_related("tier")
            model_loser = await User.get(discord_id=model_loser.discord_id).prefetch_related("tier")

        asker_won = view.winner_id == asker_user.id

        new_tier = model_winner.tier
        winner_rank_up = False
        if old_tier.pk != new_tier.pk:
            winner_rank_up = True

        try:
            await asker_user.edit(nickname=await get_match_score_nickname(asker_user.global_name or asker_user.username, model_winner if asker_won else model_loser))
            await opposer_user.edit(nickname=await get_match_score_nickname(opposer_user.global_name or opposer_user.username,
                                                                 model_winner if not asker_won else model_loser))
        except hikari.ForbiddenError:
            print("Couldn't edit one or more debate participant nicknames")

        embed = hikari.Embed(title=f"{secret.COMPLETED_DEBATE_EMOJI} Match Finished", color=0xFF0000)
        attachment = hikari.Bytes(await asyncio.to_thread(
            generate_debate_image,
            "match_complete_wleft.png" if asker_won else "match_complete_wright.png",
            await build_nonasync_member(asker_user),
            await build_nonasync_member(opposer_user),
            await build_nonasync_member(ctx.member),
            NonAsyncMatch(match_obj, winner_elo_change, loser_elo_change)
        ), "output.png")
        embed.add_field(name="Topic", value=match_obj.topic)
        embed.add_field(name="End Time", value=f"<t:{int(incomplete_match_obj.started_at.timestamp())}:F>")
        duration_seconds = int((incomplete_match_obj.ended_at - incomplete_match_obj.started_at).total_seconds())
        embed.add_field(name="Duration", value=f"{duration_seconds // 3600} hours and {(duration_seconds % 3600) // 60} minutes", inline=True)
        embed.add_field(name="Stance Description", value=match_obj.stance_description)
        embed.add_field(name="Additional Notes", value=match_obj.additional_notes)
        asker_elo_change = f"+{winner_elo_change}" if asker_won else f"{loser_elo_change}"
        opposer_elo_change = f"{loser_elo_change}" if asker_won else f"+{winner_elo_change}"
        embed.add_field(name=f"Elo Change ({asker_user.username})", value=asker_elo_change, inline=True)
        embed.add_field(name=f"Elo Change ({opposer_user.username})", value=opposer_elo_change, inline=True)

        await ctx.respond(attachment=attachment, embed=embed)

        if winner_rank_up:
            await ctx.respond(content=f"{asker_user.mention if asker_won else opposer_user.mention} ranked up!", attachment=hikari.Bytes(
                await asyncio.to_thread(
                    generate_tier_rankup_image,
                    await build_nonasync_member(asker_user if asker_won else opposer_user),
                    old_tier,
                    new_tier,
                ), "output.png"))

def generate_debate_image(template: str, user_1: NonAsyncHikariMember, user_2: NonAsyncHikariMember, judge: NonAsyncHikariMember, match: NonAsyncMatch | None = None):
    background = Editor(f"images/{template}")
    user_1_avatar = Editor(user_1.avatar_bytes)
    user_2_avatar = Editor(user_2.avatar_bytes)
    judge_avatar = Editor(judge.avatar_bytes)

    user_1_avatar.resize((288, 288)).circle_image()
    background.paste(user_1_avatar, (205, 152))
    user_2_avatar.resize((288, 288)).circle_image()
    background.paste(user_2_avatar, (788, 152))

    judge_font = Font("images/Assistant-Regular.ttf", size=24)
    background.text(
        position=(654, 593),
        text=judge.display_name,
        font=judge_font,
        color="white",
        align="center"
    )
    judge_icon_x = int(654 - (judge_font.font.getlength(judge.display_name) / 2) - 40 - 10)
    judge_avatar.resize((40, 40)).circle_image()
    background.paste(judge_avatar, (judge_icon_x, 585))

    for curr_user, pos_x in [(user_1, 349), (user_2, 932)]:
        background.text(
            position=(pos_x, 455),
            text=curr_user.display_name,
            font=Font("images/Assistant-Regular.ttf", size=28),
            color="white",
            align="center"
        )

        tier_font = Font("images/Assistant-SemiBold.ttf", size=24)
        if curr_user.tier.icon:
            tier_icon = Editor(curr_user.tier.icon)
            tier_icon.resize((30, 30))
            icon_x = int(pos_x - (tier_font.font.getlength(curr_user.tier.name) / 2) - 30 - 10)
            background.paste(tier_icon, (icon_x, 485))

        background.text(
            position=(pos_x, 488),
            text=curr_user.tier.name,
            font=tier_font,
            color=curr_user.tier.color,
            align="center"
        )

        elo_font = Font("images/Assistant-SemiBold.ttf", size=20)
        elo_text = f"{curr_user.user.elo} Elo"

        background.text(
            position=(pos_x, 515),
            text=elo_text,
            font=elo_font,
            color="#eeff41",
            align="center"
        )

        if match:
            background.text(
                position=(pos_x + int((elo_font.font.getlength(elo_text))) + 5, 515),
                text=f"+{match.elo_winner_change}" if curr_user.user.discord_id == match.match_obj.winner.discord_id else f"{match.elo_loser_change}",
                font=elo_font,
                color="#eeff41" if curr_user.user.discord_id == match.match_obj.winner.discord_id else "#fc1703",
                align="left"
            )

    return background.image_bytes.getvalue()

def generate_tier_rankup_image(user: NonAsyncHikariMember, from_tier: Tier, to_tier: Tier) -> bytes:
    background = Editor("images/tier_rankup.png")
    from_tier_image = Editor(from_tier.icon).resize((256, 256))
    to_tier_image = Editor(to_tier.icon).resize((256, 256))
    user_avatar = Editor(user.avatar_bytes).circle_image().resize((64, 64))
    background.paste(from_tier_image, (248 - 128, 512 + 15 - 128))
    background.paste(to_tier_image, (777 - 128, 512 + 15 - 128))

    user_elo_font = Font("images/Assistant-SemiBold.ttf", size=24)
    user_elo_text = f"{user.display_name} (Elo: {user.user.elo})"
    background.text(
        position=(544, 750),
        text=user_elo_text,
        font=user_elo_font,
        color="#eeff41",
        align="center"
    )
    background.paste(user_avatar, (544 - int(user_elo_font.font.getlength(user_elo_text) / 2) - 84, 730))

    return background.image_bytes.getvalue()

def generate_lb_image(stats_dict: "dict[int, dict[str, str | Match | Tier | NonAsyncHikariMember]]", start_page: int, top_3: bool = False) -> bytes:
    start_pos = start_page*8
    background = Editor("images/leaderboard_t3.png" if top_3 else "images/leaderboard_blank.png")
    start_x = 39
    start_idx = [36, 156, 276, 396, 466, 536, 606, 676] if top_3 else [0, 100, 200, 300, 400, 500, 600, 700]

    for i, (user_id, stats) in enumerate(stats_dict.items()):
        y_start = start_idx[i]
        if top_3 and y_start <= 276:
            big_font = Font("images/Assistant-SemiBold.ttf", size=20)
            small_font = Font("images/Assistant-SemiBold.ttf", size=16)
            smallest_font = Font("images/Assistant-SemiBold.ttf", size=14)

            user_avatar = Editor(stats["na_member"].avatar_bytes).circle_image().resize((94, 94))
            background.paste(user_avatar, (start_x, y_start))
            background.text(
                position=(start_x + 5, y_start + 5),
                text=f"#{start_pos + i + 1}",
                font=Font("images/Ozone.ttf", size=20),
                color="#ffffff",
                align="right"
            )
            background.text(
                position=(start_x + 110, y_start + 5),
                text=stats["na_member"].display_name,
                font=big_font,
                color="#ffffff",
                align="left"
            )
            background.text(
                position=(520, y_start + 5),
                text=f"{stats["matches_participated"]} Matches",
                font=small_font,
                color="#ffffff",
                align="right"
            )
            background.text(
                position=(start_x + 110, y_start + 25),
                text=f"{stats["elo"]} Elo",
                font=small_font,
                color="#eeff41",
                align="left"
            )

            background.text(
                position=(start_x + 110, y_start + 45),
                text=stats["tier"].name,
                font=small_font,
                color=stats["tier"].color,
                align="left"
            )
            if stats["tier"].icon:
                tier_icon = Editor(stats["tier"].icon)
                tier_icon.resize((20, 20))
                icon_x = int(start_x + 115 + (small_font.font.getlength(stats["tier"].name)))
                background.paste(tier_icon, (icon_x, y_start + 43))

            background.text(
                position=(start_x + 110, y_start + 65),
                text=f"W/L: {stats["matches_won"]}-{stats["matches_lost"]}",
                font=small_font,
                color="#26a9eb",
                align="left"
            )
            background.text(
                position=(start_x + 110, y_start + 85),
                text=f"Last Active Match: {stats["last_match"].completed_at if stats["last_match"] else 'N/A'}",
                font=smallest_font,
                color="#ffffff",
                align="left"
            )
        else:
            big_font = Font("images/Assistant-SemiBold.ttf", size=18)
            small_font = Font("images/Assistant-SemiBold.ttf", size=14)
            smallest_font = Font("images/Assistant-SemiBold.ttf", size=12)

            user_avatar = Editor(stats["na_member"].avatar_bytes).circle_image().resize((48, 48))
            background.paste(user_avatar, (start_x + 24, y_start + 20))
            background.text(
                position=(start_x + 20, y_start + 15),
                text=f"#{start_pos + i + 1}",
                font=Font("images/Ozone.ttf", size=18),
                color="#ffffff",
                align="right"
            )
            background.text(
                position=(start_x + 110, y_start + 20),
                text=stats["na_member"].display_name,
                font=big_font,
                color="#ffffff",
                align="left"
            )
            background.text(
                position=(530, y_start + 20),
                text=f"{stats["matches_participated"]} Matches",
                font=small_font,
                color="#ffffff",
                align="right"
            )
            background.text(
                position=(start_x + 110, y_start + 40),
                text=f"{stats["elo"]} Elo",
                font=small_font,
                color="#eeff41",
                align="left"
            )

            background.text(
                position=(start_x + 110, y_start + 55),
                text=stats["tier"].name,
                font=small_font,
                color=stats["tier"].color,
                align="left"
            )
            if stats["tier"].icon:
                tier_icon = Editor(stats["tier"].icon)
                tier_icon.resize((20, 20))
                icon_x = int(start_x + 115 + (small_font.font.getlength(stats["tier"].name)))
                background.paste(tier_icon, (icon_x, y_start + 52))

            background.text(
                position=(530, y_start + 37),
                text=f"W/L: {stats["matches_won"]}-{stats["matches_lost"]}",
                font=small_font,
                color="#26a9eb",
                align="right"
            )
            background.text(
                position=(530, y_start + 55),
                text=f"Last Active Match: {stats["last_match"].completed_at if stats["last_match"] else 'N/A'}",
                font=smallest_font,
                color="#ffffff",
                align="right"
            )

    return background.image_bytes.getvalue()

def generate_match_history_image(matches: list[NonAsyncMatch], na_user: NonAsyncHikariMember | None = None) -> bytes:
    background = Editor("images/match_history.png")
    y_start = [80, 200, 320, 440, 560, 680]
    big_font = Font("images/Assistant-SemiBold.ttf", size=20)

    for i, m in enumerate(matches[:6]):
        if na_user is None:
            background.text(
                text=f"#{m.match_obj.id}",
                position=(25, y_start[i]),
                font=big_font,
                color="#ffffff",
                align="left"
            )
        elif m.match_obj.draw or m.match_obj.nulled:
            background.text(
                text="Draw/Nulled",
                position=(25, y_start[i]),
                font=big_font,
                color="#ffffff",
                align="left"
            )
        elif m.match_obj.winner.discord_id == na_user.user.discord_id:
            background.text(
                text="Win",
                position=(25, y_start[i]),
                font=big_font,
                color="#eeff41",
                align="left"
            )
            background.text(
                text=f"+{m.elo_winner_change}",
                position=(530, y_start[i]),
                font=big_font,
                color="#eeff41",
                align="right"
            )
        else:
            background.text(
                text="Loss",
                position=(25, y_start[i]),
                font=big_font,
                color="#ff3838",
                align="left"
            )
            background.text(
                text=f"{m.elo_loser_change}",
                position=(530, y_start[i]),
                font=big_font,
                color="#ff3838",
                align="right"
            )

        background.paste(Editor(m.winner_na.avatar_bytes).circle_image().resize((48, 48)), (25, y_start[i] + 30))
        background.paste(Editor(m.loser_na.avatar_bytes).circle_image().resize((48, 48)), (150, y_start[i] + 30))
        background.text(
            text="vs",
            position=(108, y_start[i] + 50),
            font=Font("images/Assistant-SemiBold.ttf", size=14),
            color="#26a9eb",
            align="center"
        )
        background.text(
            text=m.winner_na.display_name,
            position=(50, y_start[i] + 85),
            font=Font("images/Assistant-SemiBold.ttf", size=12),
            color = "#ffffff",
            align = "center"
        )
        background.text(
            text=m.loser_na.display_name,
            position=(175, y_start[i] + 85),
            font=Font("images/Assistant-SemiBold.ttf", size=12),
            color="#ffffff",
            align="center"
        )

    return background.image_bytes.getvalue()

class JudgeDebateInputModal(miru.Modal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    stance_description = miru.TextInput(label="Stance Description", placeholder="Briefly explain each person's stance in this debate", required=True, style=hikari.TextInputStyle.PARAGRAPH)
    additional_notes = miru.TextInput(label="Additional notes", style=hikari.TextInputStyle.PARAGRAPH)

    async def callback(self, ctx: miru.ModalContext) -> None:
        await ctx.defer()

class JudgeDebateInputModalView(DisableView):
    def __init__(self, judge: hikari.Member, user_1: hikari.Member, user_2: hikari.Member, *args, **kwargs) -> None:
        super().__init__(timeout=120, *args, **kwargs)

        self.judge = judge
        self.user_1 = user_1
        self.user_2 = user_2
        self.stance_description = ""
        self.additional_notes = ""

        self.select = miru.TextSelect(
            placeholder="Choose a winner",
            options=[
                miru.SelectOption(label=self.user_1.display_name, value=str(self.user_1.id)),
                miru.SelectOption(label=self.user_2.display_name, value=str(self.user_2.id)),
            ]
        )

        self.select.on_select = self.on_select
        self.add_item(self.select)
        self.winner_id = ""

    async def view_check(self, ctx: miru.ViewContext) -> bool:
        if not ctx.member.id == self.judge.id:
            await ctx.respond("This prompt isn't for you!", flags=hikari.MessageFlag.EPHEMERAL)
            return False

        return True

    @miru.button(label="Finish debate", style=hikari.ButtonStyle.PRIMARY)
    async def finish_btn(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if self.select.values:
            self.winner_id = self.select.values[0]
        else:
            await ctx.respond("You need to select a winning user first!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        modal = JudgeDebateInputModal(title="Finish Debate")
        await ctx.respond_with_modal(modal)
        await modal.wait(timeout=700)

        self.stance_description = modal.stance_description.value
        self.additional_notes = modal.additional_notes.value

        await self.disable_and_stop_ui()


    async def on_timeout(self) -> None:
        await self.disable_and_stop_ui()
        self.stop()

    async def on_select(self, ctx: miru.ViewContext, select: miru.TextSelect) -> None:
        await ctx.defer()

class LimitedAcceptDenyView(DisableView):
    def __init__(self, asker: hikari.User, user_requested: hikari.User, user_request_str: str, *args, **kwargs):
        super().__init__(timeout=120, *args, **kwargs)

        self.asker = asker
        self.user_requested = user_requested
        self.user_request_str = user_request_str
        self.answer = None

    @miru.button(label="Accept", style=hikari.ButtonStyle.SUCCESS)
    async def accept(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.user_requested.id:
            await ctx.respond(f"Only the requested {self.user_request_str} can accept!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = True
        await self.disable_and_stop_ui()

    @miru.button(label="Deny", style=hikari.ButtonStyle.DANGER)
    async def deny(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.user_requested.id:
            await ctx.respond(f"Only the requested {self.user_request_str} can deny!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = False
        await self.disable_and_stop_ui()


    @miru.button(label="Cancel (for asker)", style=hikari.ButtonStyle.SECONDARY)
    async def cancel(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if ctx.user.id != self.asker.id:
            await ctx.respond(f"Only the asker ({self.asker.mention}) can cancel!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        self.answer = "cancel"
        await self.disable_and_stop_ui()

    async def on_timeout(self) -> None:
        await self.disable_and_stop_ui()
        self.stop()


@lb_client.register
class MatchInfo(lightbulb.SlashCommand, name="matchinfo", description="Get information about a completed match"):
    match_id = lightbulb.number("match_id", "Id of the match you want to lookup (retrieve using global /matches)")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context):
        match_obj = await Match.get(id=self.match_id).prefetch_related("winner", "loser", "judge")
        embed = hikari.Embed(title="Match Info")
        embed.add_field("One-liner", str(match_obj))
        embed.add_field("Stance Description (by judge)", match_obj.stance_description)
        embed.add_field("Additional Notes (by judge)", match_obj.additional_notes)
        embed.add_field("Draw/Null", match_obj.draw or match_obj.nulled)
        embed.add_field("Completed at", f"<t:{int(match_obj.completed_at.timestamp())}:F>")
        embed.add_field("Winner elo change", f"+{await match_obj.get_winner_elo_change()}")
        embed.add_field("Loser elo change", f"{await match_obj.get_loser_elo_change()}")
        embed.add_field("Predicted winner probability", f"{await match_obj.get_expected_player_winner_prob()}")

        await ctx.respond(embed=embed)

@lb_client.register
class Matches(lightbulb.SlashCommand, name="matches", description="View match history globally or for a specific user"):
    user = lightbulb.user("user", "The user you want to view match history for", default=None)
    from_pos = lightbulb.number("page_number", "The position to start the match history", default=0)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, target_user: hikari.User | None = None) -> None:
        await ctx.defer(ephemeral=True)

        single_user = self.user or target_user
        model_user = None

        if single_user:
            try:
                model_user = await User.get(discord_id=single_user.id)
            except DoesNotExist:
                await ctx.respond("User not found in the database.", ephemeral=True)
                return

        view = MatchesHistoryView(
            start_page=int(self.from_pos),
            guild_id=ctx.guild_id,
            user=model_user
        )

        response_id = await ctx.respond(content="Loading...", components=view)
        miru_client.start_view(view)
        await view.update(ctx, response_id=response_id)


class MatchesHistoryView(DisableView):
    def __init__(self, start_page: int = 0, guild_id: int | None = None, user: User | None = None):
        super().__init__(timeout=180)
        self.page = start_page
        self.page_size = 6
        self.guild_id = guild_id
        self.user = user
        self._lock = asyncio.Lock()

    def _get_button(self, label: str):
        return next(
            item for item in self.children
            if isinstance(item, miru.Button) and item.label == label
        )

    async def fetch_page(self):
        query = Match.all()
        if self.user:
            query = Match.filter(Q(winner=self.user) | Q(loser=self.user))

        total = await query.count()
        matches = await query.prefetch_related("winner", "loser").order_by("-completed_at")[
                        self.page * self.page_size: self.page * self.page_size + self.page_size
                        ]
        return matches, total

    async def build_matches(self, matches):
        na_matches = []
        for m in matches:
            winner_member = bot.cache.get_member(self.guild_id, m.winner.discord_id) or await bot.rest.fetch_member(
                self.guild_id, m.winner.discord_id)
            loser_member = bot.cache.get_member(self.guild_id, m.loser.discord_id) or await bot.rest.fetch_member(
                self.guild_id, m.loser.discord_id)

            na_matches.append(NonAsyncMatch(
                match_obj=m,
                elo_winner_change=await m.get_winner_elo_change(),
                elo_loser_change=await m.get_loser_elo_change(),
                winner_na=await build_nonasync_member(winner_member),
                loser_na=await build_nonasync_member(loser_member)
            ))
        return na_matches

    async def render(self):
        matches, total = await self.fetch_page()
        na_matches = await self.build_matches(matches)

        na_user = None
        if self.user:
            member = bot.cache.get_member(self.guild_id, self.user.discord_id) or await bot.rest.fetch_member(
                self.guild_id, self.user.discord_id)
            na_user = await build_nonasync_member(member)

        image = await asyncio.to_thread(
            generate_match_history_image,
            na_matches,
            na_user
        )

        embed = hikari.Embed(
            title=f"Match History (Page {self.page + 1})",
            description=f"Showing {self.page * self.page_size + 1}-{self.page * self.page_size + len(matches)} / {total}",
        )

        prev_button = self._get_button("◀ Prev")
        next_button = self._get_button("Next ▶")
        prev_button.disabled = self.page == 0
        next_button.disabled = (self.page + 1) * self.page_size >= total

        return embed, image

    async def update(self, ctx, response_id=None):
        async with self._lock:
            kwargs = dict(
                content="Loading...",
                components=self,
            )

            if response_id is None:
                await ctx.edit_response(**kwargs)
            else:
                await ctx.edit_response(response_id=response_id, **kwargs)

            await asyncio.sleep(0.2)

            embed, image = await self.render()
            kwargs = dict(
                content=None,
                embed=embed,
                attachment=hikari.Bytes(image, "matches.png"),
                components=self,
            )

            if response_id is None:
                await ctx.edit_response(**kwargs)
            else:
                await ctx.edit_response(response_id=response_id, **kwargs)

    @miru.button(label="◀ Prev", style=hikari.ButtonStyle.SECONDARY)
    async def prev(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()
        async with self._lock:
            if self.page > 0:
                self.page -= 1
            await self.update(ctx)

    @miru.button(label="Next ▶", style=hikari.ButtonStyle.SECONDARY)
    async def next(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()
        async with self._lock:
            self.page += 1
            await self.update(ctx)

    @miru.button(label="⏹ Stop", style=hikari.ButtonStyle.DANGER)
    async def stop_btn(self, ctx: miru.ViewContext, button: miru.Button):
        await ctx.defer()
        await self.disable_and_stop_ui()

    async def on_timeout(self):
        await self.disable_and_stop_ui()


@lb_client.register
class GetUserMatches(lightbulb.UserCommand, name="View match history"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.defer(ephemeral=True)
        try:
            model_user = await User.get(discord_id=self.target.id)
        except DoesNotExist:
            await ctx.respond("User not found in the database.", ephemeral=True)
            return

        view = MatchesHistoryView(
            start_page=0,
            guild_id=ctx.guild_id,
            user=model_user
        )

        response_id = await ctx.respond(content="Loading...", components=view)
        miru_client.start_view(view)

        await view.update(ctx, response_id=response_id)

bot.subscribe(hikari.StartingEvent, start_db)
bot.subscribe(hikari.StartingEvent, lb_client.start)
lb_client.register(debate)
bot.run()
