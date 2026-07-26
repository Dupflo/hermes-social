from app.webhook_parser import CommentEvent


PLATFORM_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "facebook_dm": "Message privé Facebook",
    "instagram_dm": "Message privé Instagram",
}


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def facebook_graph_comment_id(comment_id: str) -> str:
    """Return the short Facebook comment ID used by public comment actions.

    Meta often gives compound IDs like `post_id_comment_id` in webhooks and
    backfills. Public actions such as `/{comment-id}/comments` generally expect
    only the final comment segment, while the full compound ID is kept for local
    idempotency and private reply payloads.
    """

    return comment_id.rsplit("_", 1)[1] if "_" in comment_id else comment_id


def is_owner_identity(
    *,
    author_id: str | None,
    username: str | None,
    owner_ids: set[str],
    owner_usernames: set[str],
) -> bool:
    if author_id and author_id in owner_ids:
        return True
    return bool(username and username.lower() in {name.lower() for name in owner_usernames})


def is_owner_comment_event(event: CommentEvent, owner_ids: set[str], owner_usernames: set[str]) -> bool:
    return is_owner_identity(
        author_id=event.author_id,
        username=event.username,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
    )


def parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def owner_identity_sets(settings) -> tuple[set[str], set[str]]:
    """Return configured owner IDs/usernames for Meta-originated events.

    Includes the Page ID and Instagram user ID by default, then merges optional
    comma-separated environment values:
    - META_OWNER_IDS
    - META_OWNER_USERNAMES
    """

    owner_ids = parse_csv_set(getattr(settings, "meta_owner_ids", ""))
    owner_ids.update(parse_csv_set(getattr(settings, "meta_page_id", "")))
    owner_ids.update(parse_csv_set(getattr(settings, "meta_ig_user_id", "")))
    owner_usernames = parse_csv_set(getattr(settings, "meta_owner_usernames", ""))
    return owner_ids, owner_usernames
