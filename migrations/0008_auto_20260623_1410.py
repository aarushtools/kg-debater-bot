from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0007_auto_20260604_1151')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Match',
            name='completed_at',
            field=fields.DatetimeField(auto_now=False, auto_now_add=True),
        ),
    ]
