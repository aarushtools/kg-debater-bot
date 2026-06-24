from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0006_auto_20260602_1849')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Tier',
            name='icon',
            field=fields.BinaryField(null=True),
        ),
    ]
