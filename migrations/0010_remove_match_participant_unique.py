from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields


class Migration(migrations.Migration):
    dependencies = [('botdb', '0009_add_admin_action')]

    initial = False

    operations = [
        ops.AlterField(
            model_name='IncompleteMatch',
            name='asker',
            field=fields.ForeignKeyField('botdb.User', source_field='asker_id', db_constraint=True, to_field='discord_id', related_name='incomplete_matches_asked', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='IncompleteMatch',
            name='opposer',
            field=fields.ForeignKeyField('botdb.User', source_field='opposer_id', null=True, db_constraint=True, to_field='discord_id', related_name='incomplete_matches_opposed', on_delete=OnDelete.SET_NULL),
        ),
        ops.AlterField(
            model_name='IncompleteMatch',
            name='judge',
            field=fields.ForeignKeyField('botdb.User', source_field='judge_id', null=True, db_constraint=True, to_field='discord_id', related_name='incomplete_matches_judged', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='Match',
            name='winner',
            field=fields.ForeignKeyField('botdb.User', source_field='winner_id', db_constraint=True, to_field='discord_id', related_name='matches_won', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='Match',
            name='loser',
            field=fields.ForeignKeyField('botdb.User', source_field='loser_id', db_constraint=True, to_field='discord_id', related_name='matches_lost', on_delete=OnDelete.RESTRICT),
        ),
        ops.AlterField(
            model_name='Match',
            name='judge',
            field=fields.ForeignKeyField('botdb.User', source_field='judge_id', db_constraint=True, to_field='discord_id', related_name='matches_judged', on_delete=OnDelete.RESTRICT),
        ),
    ]
