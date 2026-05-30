from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0002_auto_20260530_1502')]

    initial = False

    operations = [
        ops.CreateModel(
            name='IncompleteMatch',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('asker', fields.ForeignKeyField('botdb.User', source_field='asker_id', unique=True, db_constraint=True, to_field='discord_id', related_name='incomplete_matches_asked', on_delete=OnDelete.RESTRICT)),
                ('opposer', fields.ForeignKeyField('botdb.User', source_field='opposer_id', null=True, unique=True, db_constraint=True, to_field='discord_id', related_name='incomplete_matches_opposed', on_delete=OnDelete.SET_NULL)),
                ('judge', fields.ForeignKeyField('botdb.User', source_field='judge_id', null=True, unique=True, db_constraint=True, to_field='discord_id', related_name='incomplete_matches_judged', on_delete=OnDelete.RESTRICT)),
                ('topic', fields.CharField(max_length=255)),
                ('ongoing', fields.BooleanField(default=True)),
                ('started', fields.BooleanField(default=False)),
            ],
            options={'table': 'incompletematch', 'app': 'botdb', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.AlterField(
            model_name='Match',
            name='judge',
            field=fields.ForeignKeyField('botdb.User', source_field='judge_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_judged', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='Match',
            name='loser',
            field=fields.ForeignKeyField('botdb.User', source_field='loser_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_lost', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='Match',
            name='winner',
            field=fields.ForeignKeyField('botdb.User', source_field='winner_id', unique=True, db_constraint=True, to_field='discord_id', related_name='matches_won', on_delete=OnDelete.RESTRICT),
        ),
        ops.AddField(
            model_name='Match',
            name='topic',
            field=fields.CharField(max_length=255),
        ),
        ops.RemoveField(model_name='Match', name='ongoing'),
    ]
