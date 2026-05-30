from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0001_initial')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Match',
            name='ongoing',
            field=fields.BooleanField(default=True),
        ),
    ]
