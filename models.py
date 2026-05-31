import hikari
from tortoise.exceptions import ValidationError, DoesNotExist
from tortoise import Tortoise, Model, fields, models
from tortoise.expressions import Q
from tortoise.manager import Manager
import secret


async def start_db(event: hikari.StartingEvent) -> None:
    if not Tortoise.is_inited():
        await Tortoise.init(config=secret.TORTOISE_ORM, _enable_global_fallback=True)

class TierManager(Manager):
    async def get_default_tier(self) -> "Tier":
        return await super().get_queryset().get(elo_min=0)

class Tier(Model):
    name = fields.CharField(max_length=255)
    elo_min = fields.IntField()
    elo_max = fields.IntField()
    role_id = fields.CharField(max_length=255)

    async def save(self, *args, **kwargs) -> None:
        # Check if this tier is overlapping the elo of any other tiers
        tier_overlaps = await Tier.filter(
            (Q(elo_min__range=[self.elo_min, self.elo_max]) | Q(elo_max__range=[self.elo_min, self.elo_max]))
        ).exclude(id=self.id)

        if tier_overlaps:
            raise ValidationError("This tier overlaps with " + ", ".join([t.name for t in tier_overlaps]))

        # Check if any tier (or this one) has elo 0 so default tier can be assigned
        try:
            _ = self.elo_min == 0 or self.objects.get_default_tier()
        except DoesNotExist:
            raise ValidationError("Changing this tier removes the default tier (elo = 0), or "
                                  "there was never one in the first place?")

        await super().save(*args, **kwargs)

    objects = TierManager()

    def __str__(self):
        return f"{self.name} ({self.elo_min} - {self.elo_max})"

class User(Model):
    discord_id = fields.CharField(primary_key=True, max_length=50)
    discord_name = fields.CharField(max_length=255)  # TODO: Refreshes every 24 hours
    elo = fields.IntField(default=0)
    tier = fields.ForeignKeyField("botdb.Tier", on_delete=fields.CASCADE, related_name="users")

    async def calculate_dynamic_tier_object(self) -> Tier:
        """Returns the Tier object for this user through a fresh query instead of the foreign key
        based on current elo amount"""
        return await Tier.get(elo_min__lte=self.elo, elo_max__gte=self.elo)

    async def get_match_score(self) -> tuple[int, int]:
        """Returns the match score for this user in the format [wins, losses]"""
        wins = await self.matches_won.all().count()
        losses = await self.matches_lost.all().count()

        return wins, losses

    async def is_admin(self) -> bool:
        pass

    def __str__(self):
        return self.discord_name

class IncompleteMatch(Model):
    asker = fields.ForeignKeyField("botdb.User", on_delete=fields.RESTRICT, related_name="incomplete_matches_asked", unique=True)
    opposer = fields.ForeignKeyField("botdb.User", on_delete=fields.SET_NULL, related_name="incomplete_matches_opposed", unique=True, null=True)
    judge = fields.ForeignKeyField("botdb.User", on_delete=fields.RESTRICT, related_name="incomplete_matches_judged", unique=True, null=True)
    topic = fields.CharField(max_length=255)
    ongoing = fields.BooleanField(default=True)
    started = fields.BooleanField(default=False)
    started_at = fields.DatetimeField(null=True)
    ended_at = fields.DatetimeField(null=True)

    def __str__(self):
        """Before calling string on this model, make sure you prefetch_related asker, opposer, and judge"""
        if self.ongoing: emoji = f"{secret.ONGOING_DEBATE_EMOJI} Ongoing debate"
        else: emoji = f"{secret.COMPLETED_DEBATE_EMOJI} Completed debate"

        return f"{emoji} on '{self.topic}.' Asker: {self.asker}, Opposer: {self.opposer if self.opposer else 'TBA'}, Judge: {self.judge}"

class Match(Model):
    winner = fields.ForeignKeyField("botdb.User", on_delete=fields.RESTRICT, related_name="matches_won", unique=True)
    loser = fields.ForeignKeyField("botdb.User", on_delete=fields.RESTRICT, related_name="matches_lost", unique=True)
    judge = fields.ForeignKeyField("botdb.User", on_delete=fields.RESTRICT, related_name="matches_judged", unique=True)
    topic = fields.CharField(max_length=255)
    nulled = fields.BooleanField(default=False)

    async def get_winner_elo_change(self) -> int:
        pass

    async def get_loser_elo_change(self) -> int:
        pass

    async def annul(self):
        self.nulled = True
        winner_user = await self.winner
        loser_user = await self.loser

        winner_user -= await self.get_winner_elo_change()
        loser_user += await self.get_loser_elo_change()

        await self.winner.save()
        await self.loser.save()
        await self.save()