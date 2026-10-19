"""Attachment identity metadata retained while inbound media events merge."""
from gateway.platforms.event import MessageEvent


def _merge_pending_media_metadata(existing: MessageEvent, incoming: MessageEvent) -> None:
    """Merge only attachment identity metadata that remains valid after media concatenation."""
    incoming_metadata = incoming.metadata if isinstance(incoming.metadata, dict) else {}
    values = incoming_metadata.get("discord_path_only_attachment_paths")
    if not isinstance(values, list) or not values:
        return
    if not isinstance(existing.metadata, dict):
        existing.metadata = {}
    current = existing.metadata.setdefault("discord_path_only_attachment_paths", [])
    if not isinstance(current, list):
        current = []
        existing.metadata["discord_path_only_attachment_paths"] = current
    for value in values:
        if value not in current:
            current.append(value)
