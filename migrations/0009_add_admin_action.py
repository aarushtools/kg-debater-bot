from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields


class Migration(migrations.Migration):
    dependencies = [('botdb', '0008_auto_20260623_1410')]

    initial = False

    operations = [
        ops.CreateModel(
            name='AdminAction',
            fields=[
                ('id', fields.IntField(pk=True)),
                ('title', fields.CharField(max_length=255)),
                ('description', fields.TextField()),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
        ),
    ]
