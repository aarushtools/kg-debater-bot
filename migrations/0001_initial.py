from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='Tier',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('name', fields.CharField(max_length=255)),
                ('elo_min', fields.IntField()),
                ('elo_max', fields.IntField()),
                ('role_id', fields.CharField(max_length=255)),
            ],
            options={'table': 'tier', 'app': 'botdb', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='User',
            fields=[
                ('discord_id', fields.CharField(primary_key=True, unique=True, db_index=True, max_length=50)),
                ('discord_name', fields.CharField(max_length=255)),
                ('elo', fields.IntField(default=0)),
                ('tier', fields.ForeignKeyField('botdb.Tier', source_field='tier_id', db_constraint=True, to_field='id', related_name='users', on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'user', 'app': 'botdb', 'pk_attr': 'discord_id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Match',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('winner', fields.ForeignKeyField('botdb.User', source_field='winner_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_won', on_delete=OnDelete.CASCADE)),
                ('loser', fields.ForeignKeyField('botdb.User', source_field='loser_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_lost', on_delete=OnDelete.CASCADE)),
                ('judge', fields.ForeignKeyField('botdb.User', source_field='judge_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_judged', on_delete=OnDelete.CASCADE)),
                ('nulled', fields.BooleanField(default=False)),
            ],
            options={'table': 'match', 'app': 'botdb', 'pk_attr': 'id'},
            bases=['Model'],
        ),
    ]
