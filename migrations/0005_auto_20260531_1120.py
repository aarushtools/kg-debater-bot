from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0004_auto_20260531_0049')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Match',
            name='draw',
            field=fields.BooleanField(default=False),
        ),
    ]
