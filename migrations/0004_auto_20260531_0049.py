from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0003_auto_20260530_1640')]

    initial = False

    operations = [
        ops.AddField(
            model_name='IncompleteMatch',
            name='ended_at',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
        ops.AddField(
            model_name='IncompleteMatch',
            name='started_at',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
    ]
