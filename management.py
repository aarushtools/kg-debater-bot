import hikari
from hikari import Snowflake

import secret

bot = hikari.GatewayBot(token=secret.TOKEN, intents=hikari.Intents.ALL)

async def init_users_table():
    await bot.request_guild_members(int(secret.GUILD_ID))

@bot.listen(hikari.MemberChunkEvent)
async def on_member_chunk(event: hikari.MemberChunkEvent) -> None:
    for member in event.members:
        pass
