"""Small offline fallback for the flat YAML frontmatter emitted by this plugin.

PyYAML, when installed, remains the full YAML parser. This fallback accepts string
scalars and lists of strings; it deliberately rejects nested mappings and YAML tags.
"""

import ast
import json
import re


def _scalar(value):
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        parsed = json.loads(value)
    elif value.startswith("'"):
        if not value.endswith("'"):
            raise ValueError("unterminated quoted scalar")
        parsed = value[1:-1].replace("''", "'")
    elif value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = ast.literal_eval(value)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError("frontmatter lists must contain strings")
        return parsed
    elif value.startswith(("{", "!", "&", "*", "|", ">")) or ": " in value:
        raise ValueError("complex YAML requires PyYAML")
    else:
        return value
    if not isinstance(parsed, str):
        raise ValueError("frontmatter scalars must be strings")
    return parsed


def safe_load(text):
    result = {}
    key = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if key is None or not isinstance(result[key], list):
                raise ValueError("list item without a list key")
            result[key].append(_scalar(stripped[2:]))
            continue
        match = re.fullmatch(r"([a-z_]+):(?:[ \t]*(.*))?", line)
        if not match:
            raise ValueError("expected a flat frontmatter mapping")
        key, value = match.groups()
        if key in result:
            raise ValueError("duplicate frontmatter key")
        result[key] = _scalar(value) if value else []
    return result


def safe_dump(data, **kwargs):
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend("- " + json.dumps(item, ensure_ascii=False) for item in value)
        else:
            lines.append(f"{key}: " + json.dumps(value, ensure_ascii=False))
    return "\n".join(lines) + "\n"
