from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('botdb', '0010_remove_match_participant_unique')]

    initial = False

    operations = [
        ops.AddField(
            model_name='User',
            name='is_active',
            field=fields.BooleanField(default=True),
        ),
    ]
