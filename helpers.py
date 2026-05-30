import models


async def get_match_score_nickname(current_name: str, user: models.User):
    wins, losses = await user.get_match_score()
    record_suffix = f"({wins}-{losses})"
    if len(current_name) + len(record_suffix) > 31:
        # Prevent nickname with record from exceeding discord nickname character limit
        current_name = current_name[:31 - len(record_suffix)]

    return f"{current_name} {record_suffix}"