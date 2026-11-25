from .spotify import show_id


def validate_shows(news, podcasts):
    result = []
    seen = set()
    total = 0
    for group in (news, podcasts):
        if not isinstance(group, list):
            raise ValueError("News and podcast shows must be lists")
        validated = []
        for entry in group:
            if not isinstance(entry, dict) or not isinstance(entry.get("show"), str):
                raise ValueError("Each show must have a Spotify show URL or ID and an episode count")
            count = entry.get("count")
            if type(count) is not int or count < 1:
                raise ValueError("Each show must have a positive whole-number episode count")
            sid = show_id(entry["show"])
            if sid in seen:
                raise ValueError("Each show can only be configured once")
            seen.add(sid)
            total += count
            validated.append({"show": sid, "count": count})
        result.append(validated)
    if total > 99:
        raise ValueError("Configure at most 99 episodes to leave room for music")
    return result


def parse_show_lines(text):
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2 or not parts[1].strip().isascii() or not parts[1].strip().isdigit():
            raise ValueError("Enter each show as a Spotify show URL or ID, followed by a comma and episode count")
        entries.append({"show": parts[0].strip(), "count": int(parts[1].strip())})
    return entries


def format_show_lines(shows):
    return "\n".join(f"https://open.spotify.com/show/{item['show']}, {item['count']}" for item in shows)
