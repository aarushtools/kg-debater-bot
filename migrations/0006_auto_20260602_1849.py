from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0005_auto_20260531_1120')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Match',
            name='additional_notes',
            field=fields.TextField(default='', unique=False),
        ),
        ops.AddField(
            model_name='Match',
            name='incomplete_match',
            field=fields.ForeignKeyField('botdb.IncompleteMatch', source_field='incomplete_match_id', unique=True, db_constraint=True, to_field='id', related_name='finished_match', on_delete=OnDelete.RESTRICT),
        ),
        ops.AddField(
            model_name='Match',
            name='stance_description',
            field=fields.TextField(default='', unique=False),
        ),
        ops.AddField(
            model_name='Tier',
            name='color',
            field=fields.CharField(max_length=7),
        ),
        ops.AddField(
            model_name='Tier',
            name='k_factor',
            field=fields.IntField(),
        ),
    ]
