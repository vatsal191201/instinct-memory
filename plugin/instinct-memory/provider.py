"""InstinctMemoryProvider — git-tracked markdown memory for Hermes.

Implements the architecture Dhravya Shah reverse-engineered out of Instinct:

  * the injected context is a **profile + record index**, static for the life of a session;
  * retrieval is **keyword matching with aliases**, never embeddings;
  * the agent's memory surface is **read-only** — durable changes are proposed, not written;
  * a **nightly background process** owns every record write.

The one deliberate departure: this provider is a **superset**. The user's existing
holographic SQLite fact store is composed in as a delegate so `fact_store` / `fact_feedback`
keep working when this provider becomes active. Nothing is migrated away and nothing is
deleted; the delegate is wrapped in try/except so it can never break the instinct path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus
from tools.registry import tool_error

from . import retrieval
from .schema import RECORD_TYPES, Fact, RecordError
from .vault import Vault, utc_now

logger = logging.getLogger(__name__)

DEFAULT_VAULT = "$HERMES_HOME/memory-vault"
DEFAULT_PROFILE_CHARS = 24000
DEFAULT_PREFETCH_RECORDS = 4
DEFAULT_MAX_RECORD_CHARS = 8000
PREFETCH_CACHE_SECONDS = 45.0

MEMORY_SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": (
        "Search durable memory (the instinct vault) by keyword. READ-ONLY.\n"
        "Search by the words the user actually uses — nicknames and abbreviations are stored as "
        "aliases, so 'the pm' and 'jj' find the same person as 'June'. Returns matching records "
        "with their id, type, and most relevant dated fact lines.\n"
        "Use this BEFORE answering anything about the user, their people, projects, preferences, "
        "or past decisions. Follow up with memory_read for the full record."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords, ideally the user's own wording."},
            "limit": {"type": "integer", "description": "Max records (default 8)."},
            "type": {
                "type": "string",
                "enum": list(RECORD_TYPES),
                "description": "Restrict to one record type.",
            },
        },
        "required": ["query"],
    },
}

MEMORY_READ_SCHEMA = {
    "name": "memory_read",
    "description": (
        "Read one durable memory record in full, by id or name (e.g. 'PERS-june' or 'June'). "
        "READ-ONLY. Returns frontmatter, every dated fact (including superseded ones and their "
        "corrections), and the record's links to related records."
    ),
    "parameters": {
        "type": "object",
        "properties": {"id_or_name": {"type": "string", "description": "Record id or name."}},
        "required": ["id_or_name"],
    },
}

MEMORY_INDEX_SCHEMA = {
    "name": "memory_index",
    "description": (
        "List records in durable memory — id, type, name, aliases, last updated. READ-ONLY. "
        "Use it to see what is knowable at all, or to find the right id before memory_read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(RECORD_TYPES), "description": "Restrict to one type."},
        },
    },
}

MEMORY_TIMELINE_SCHEMA = {
    "name": "memory_timeline",
    "description": (
        "Read the day/week rollups of what happened, written nightly. READ-ONLY. "
        "Use for 'what did we do last week' / 'what was going on around then' questions, "
        "where the records hold durable facts but these hold the chronology."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["daily", "weekly"], "description": "Default daily."},
            "date": {"type": "string", "description": "YYYY-MM-DD for a specific day (daily only)."},
            "limit": {"type": "integer", "description": "How many periods back (default 3)."},
        },
    },
}

MEMORY_NOTE_SCHEMA = {
    "name": "memory_note",
    "description": (
        "Flag something for durable memory. This is a PROPOSAL, not a write — it appends to an "
        "inbox the nightly reconciler reads, so it does NOT take effect immediately and cannot be "
        "read back this turn. Use it when the user says 'remember this' or when something is "
        "clearly durable and worth keeping. For a fact you need immediately, use the built-in "
        "memory tool instead; for the long-term record, use this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact or preference to remember, stated plainly."},
            "about": {
                "type": "string",
                "description": "Optional record id or name this concerns (e.g. 'June', 'PREF-coffee').",
            },
        },
        "required": ["text"],
    },
}


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        return cfg_get(load_config_readonly(), "plugins", "instinct-memory", default={}) or {}
    except Exception:
        return {}


def _expand(path: str, hermes_home: str) -> str:
    return str(path).replace("$HERMES_HOME", hermes_home).replace("${HERMES_HOME}", hermes_home)


class InstinctMemoryProvider(MemoryProvider):
    """Git-tracked markdown memory, read-only to the agent, reconciled nightly."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config if config is not None else _load_plugin_config()
        self._vault: Optional[Vault] = None
        self._session_id = ""
        self._agent_context = "primary"
        self._prompt_block: Optional[str] = None
        self._prefetch_cache: Dict[str, tuple] = {}
        self._prefetch_lock = threading.Lock()
        self._last_recall: Optional[RecallStatus] = None
        self._delegate: Optional[MemoryProvider] = None
        self._delegate_error: str = ""

    # ------------------------------------------------------------------ config

    @property
    def name(self) -> str:
        return "instinct"

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self._config.get(key, default)
        return default if value is None else value

    def is_available(self) -> bool:
        try:
            from hermes_constants import get_hermes_home

            root = _expand(str(self._cfg("vault_path", DEFAULT_VAULT)), str(get_hermes_home()))
            return Path(root).is_dir() or Path(root).parent.is_dir()
        except Exception:
            return False

    def unavailable_reason(self) -> str:
        return "instinct vault directory is missing — run the instinct-memory reconcile script once to create it."

    # -------------------------------------------------------------- lifecycle

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home

        hermes_home = str(kwargs.get("hermes_home") or get_hermes_home())
        self._session_id = session_id
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        root = Path(_expand(str(self._cfg("vault_path", DEFAULT_VAULT)), hermes_home))
        self._vault = Vault(root, max_record_chars=int(self._cfg("max_record_chars", DEFAULT_MAX_RECORD_CHARS)))
        self._vault.ensure()
        self._prompt_block = None
        self._initialize_delegate(hermes_home)

    def _initialize_delegate(self, hermes_home: str) -> None:
        """Compose the holographic SQLite store in so `fact_store` keeps working."""
        from utils import is_truthy_value

        if not is_truthy_value(self._cfg("keep_holographic", True)):
            return
        try:
            from plugins.memory.holographic import _load_plugin_config as _holo_config
            from plugins.memory.holographic import HolographicMemoryProvider

            delegate = HolographicMemoryProvider(config=_holo_config())
            delegate.initialize(self._session_id, hermes_home=hermes_home, agent_context=self._agent_context)
            self._delegate = delegate
        except Exception as exc:  # never let the delegate break the instinct path
            self._delegate = None
            self._delegate_error = str(exc)
            logger.warning("instinct: holographic delegate unavailable (%s) — continuing instinct-only", exc)

    def shutdown(self) -> None:
        if self._delegate is not None:
            try:
                self._delegate.shutdown()
            except Exception:
                pass
            self._delegate = None
        with self._prefetch_lock:
            self._prefetch_cache.clear()

    # ------------------------------------------------------------------ prompt

    def system_prompt_block(self) -> str:
        """STATIC for the life of the session (prompt caching is sacred) — assembled once."""
        if self._prompt_block is not None:
            return self._prompt_block
        if self._vault is None:
            return ""
        try:
            block = self._vault.render_profile(max_chars=int(self._cfg("profile_max_chars", DEFAULT_PROFILE_CHARS)))
        except Exception as exc:
            logger.warning("instinct: could not build profile block: %s", exc)
            return ""
        if self._delegate is not None:
            try:
                extra = self._delegate.system_prompt_block() or ""
                if extra:
                    block = block.rstrip() + "\n\n" + extra
            except Exception:
                pass
        self._prompt_block = block
        return block

    # ------------------------------------------------------------------ recall

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Fast, cached, keyword-only. No LLM, no network, ever."""
        from agent.memory_provider import is_trivial_prompt

        if self._vault is None or is_trivial_prompt(query):
            self._last_recall = None
            return ""
        key = (query.strip().lower()[:200], session_id or self._session_id)
        now = time.monotonic()
        with self._prefetch_lock:
            cached = self._prefetch_cache.get(key)
            if cached and (now - cached[0]) < PREFETCH_CACHE_SECONDS:
                block, count = cached[1], cached[2]
                # A cached MISS (empty body) must not resurrect a recall indicator: count==0
                # renders as "recalled relevant memory" while nothing was injected. Mirror the
                # fresh-miss path and report no recall.
                self._last_recall = (
                    RecallStatus(provider_label="instinct vault", count=count) if block else None
                )
                return block
        try:
            records = self._vault.load_refs()
            idf = retrieval.build_idf(records)
            hits, tokens = retrieval.search(
                records, query, limit=int(self._cfg("prefetch_records", DEFAULT_PREFETCH_RECORDS)), idf=idf
            )
        except Exception as exc:
            logger.debug("instinct prefetch failed: %s", exc)
            return ""
        if not hits:
            with self._prefetch_lock:
                self._prefetch_cache[key] = (now, "", 0)
            self._last_recall = None
            return ""
        body = "\n\n".join(retrieval.render_snippet(hit.record, tokens) for hit in hits)
        body = body[: retrieval.MAX_PREFETCH_CHARS]
        block = "## Memory (instinct vault) — retrieved for this turn\n" + body
        with self._prefetch_lock:
            self._prefetch_cache[key] = (now, block, len(hits))
            if len(self._prefetch_cache) > 128:
                self._prefetch_cache = dict(list(self._prefetch_cache.items())[-64:])
        self._last_recall = RecallStatus(provider_label="instinct vault", count=len(hits))
        return block

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Warm the cache off the hot path. Never raises."""
        if self._vault is None:
            return
        try:
            records = self._vault.load_refs()
            retrieval.build_idf(records)
        except Exception:
            pass

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # ---------------------------------------------------------------- capture

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append-only raw capture. Deliberately does no thinking: the reconciler reads this
        log later. Cheap enough to run on every turn.

        ``turn_author`` ({"id", "name", "is_bot"}) names who wrote the user side. A shared
        session carries several participants, so the reconciler must be able to tell whose
        statement it is turning into a durable fact — recording someone else's preference as
        the user's own is the failure this prevents.
        """
        if self._vault is None or self._agent_context != "primary":
            return
        if not (user_content or assistant_content):
            return
        payload = {
            "ts": utc_now(),
            "session_id": session_id or self._session_id,
            "platform": self._cfg("platform", ""),
            "user": (user_content or "")[:6000],
            "assistant": (assistant_content or "")[:6000],
        }
        if isinstance(turn_author, dict) and turn_author.get("id") is not None:
            payload["author"] = {
                "id": turn_author.get("id"),
                "name": turn_author.get("name"),
                "is_bot": bool(turn_author.get("is_bot")),
            }
        try:
            self._vault.append_raw(f"{self._day()}.jsonl", payload)
        except Exception as exc:
            logger.debug("instinct: raw append failed: %s", exc)

    def _day(self) -> str:
        from datetime import date

        return date.today().isoformat()

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in memory-tool writes into the raw log so the reconciler sees them."""
        if self._vault is None or not content:
            return
        try:
            self._vault.append_raw(
                "memory_tool_writes.jsonl",
                {"ts": utc_now(), "action": action, "target": target, "content": content[:2000], "metadata": metadata or {}},
            )
        except Exception as exc:
            logger.debug("instinct: memory_write mirror failed: %s", exc)
        if self._delegate is not None:
            try:
                self._delegate.on_memory_write(action, target, content, metadata)
            except Exception:
                pass

    # ------------------------------------------------------------------- tools

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            MEMORY_SEARCH_SCHEMA,
            MEMORY_READ_SCHEMA,
            MEMORY_INDEX_SCHEMA,
            MEMORY_TIMELINE_SCHEMA,
            MEMORY_NOTE_SCHEMA,
        ] + self._delegate_tool_schemas()

    def _delegate_tool_schemas(self) -> List[Dict[str, Any]]:
        """Delegate (holographic) tool schemas — advertised even BEFORE ``initialize()``.

        ``MemoryManager.add_provider()`` snapshots ``get_tool_schemas()`` exactly once to
        build its tool→provider routing table, and it runs BEFORE ``initialize()`` creates
        the delegate. If these schemas only appeared post-init, ``fact_store``/``fact_feedback``
        would be injected into the model's tool surface (that happens after init) yet be
        absent from the routing table — so every call would return "no provider handles this
        tool". Returning the delegate's live schemas when it exists, and the holographic
        module's static schema constants when it does not (but is enabled), keeps the
        advertised surface and the routing table in agreement.
        """
        if self._delegate is not None:
            try:
                return list(self._delegate.get_tool_schemas())
            except Exception as exc:
                logger.debug("instinct: delegate schemas unavailable: %s", exc)
                return []
        from utils import is_truthy_value

        if not is_truthy_value(self._cfg("keep_holographic", True)):
            return []
        try:
            from plugins.memory.holographic import FACT_FEEDBACK_SCHEMA, FACT_STORE_SCHEMA

            return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]
        except Exception as exc:
            logger.debug("instinct: could not preload holographic schemas: %s", exc)
            return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name in ("fact_store", "fact_feedback"):
            # These are always advertised when keep_holographic is on (see
            # _delegate_tool_schemas), so route them here even if the delegate failed to
            # initialize — with a clear error rather than a confusing "Unknown tool".
            if self._delegate is None:
                return tool_error(
                    f"holographic delegate unavailable ({self._delegate_error or 'not initialized'})"
                )
            try:
                return self._delegate.handle_tool_call(tool_name, args, **kwargs)
            except Exception as exc:
                return tool_error(f"holographic delegate failed: {exc}")
        handler = self._TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return tool_error(f"Unknown tool: {tool_name}")
        try:
            return handler(self, args)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except RecordError as exc:
            return tool_error(str(exc))
        except Exception as exc:
            logger.debug("instinct tool %s failed: %s", tool_name, exc)
            return tool_error(str(exc))

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args["query"]
        limit = int(args.get("limit", 8))
        record_type = args.get("type")
        records = self._vault.load_refs() if self._vault else []
        idf = retrieval.build_idf(records)
        hits, tokens = retrieval.search(records, query, limit=limit, record_type=record_type, idf=idf)
        if not hits:
            return json.dumps(
                {
                    "results": [],
                    "count": 0,
                    "tokens": tokens,
                    "hint": "No record matched. Try a nickname or a different phrasing; memory_index lists every record.",
                }
            )
        return json.dumps({"results": [h.to_dict(tokens) for h in hits], "count": len(hits), "tokens": tokens})

    def _tool_read(self, args: Dict[str, Any]) -> str:
        rec = self._vault.find(args["id_or_name"]) if self._vault else None
        if rec is None:
            return tool_error(f"No record matching {args['id_or_name']!r}. Use memory_index to list records.")
        linked = []
        for link in rec.links:
            target = self._vault.find(link)
            linked.append({"id": link, "name": target.name if target else None, "missing": target is None})
        return json.dumps(
            {
                "id": rec.id,
                "name": rec.name,
                "type": rec.type,
                "aliases": rec.aliases,
                "created": rec.created,
                "updated": rec.updated,
                "sources": rec.sources,
                "prose": rec.prose,
                "facts": [
                    {"line": f.render(), "current": f.is_current(), "superseded": f.superseded_on, "corrects": f.corrects_on}
                    for f in rec.facts
                ],
                "links": linked,
                "path": rec.path,
            }
        )

    def _tool_index(self, args: Dict[str, Any]) -> str:
        record_type = args.get("type")
        records = self._vault.load_refs() if self._vault else []
        if record_type:
            records = [r for r in records if r.type == record_type]
        rows = [
            {
                "id": r.id,
                "name": r.name,
                "type": r.type,
                "aliases": r.aliases,
                "updated": r.updated,
                "facts": len(r.facts),
            }
            for r in sorted(records, key=lambda r: (r.type, r.id))
        ]
        return json.dumps({"records": rows, "count": len(rows)})

    def _tool_timeline(self, args: Dict[str, Any]) -> str:
        period = args.get("period", "daily")
        limit = int(args.get("limit", 3))
        entries = self._vault.read_timeline(period, day=args.get("date"), limit=limit) if self._vault else []
        if not entries:
            return json.dumps(
                {
                    "entries": [],
                    "count": 0,
                    "hint": "No rollups written yet — the nightly reconciler creates them.",
                }
            )
        return json.dumps({"entries": entries, "count": len(entries)})

    def _tool_note(self, args: Dict[str, Any]) -> str:
        if self._vault is None:
            return tool_error("instinct vault is not initialized")
        self._vault.ensure()
        self._vault.append_raw(
            "inbox.jsonl",
            {
                "ts": utc_now(),
                "session_id": self._session_id,
                "text": args["text"],
                "about": args.get("about", ""),
            },
        )
        return json.dumps(
            {
                "status": "queued",
                "note": "Added to the reconciliation inbox. Durable as of the next reconcile run — not readable back yet.",
            }
        )

    _TOOL_HANDLERS = {
        "memory_search": _tool_search,
        "memory_read": _tool_read,
        "memory_index": _tool_index,
        "memory_timeline": _tool_timeline,
        "memory_note": _tool_note,
    }

    # ------------------------------------------------------------------ setup

    def get_config_schema(self) -> List[Dict[str, Any]]:
        from hermes_constants import display_hermes_home

        return [
            {"key": "vault_path", "description": "Path to the git-tracked memory vault", "default": f"{display_hermes_home()}/memory-vault"},
            {"key": "profile_max_chars", "description": "Cap on the injected profile + index block", "default": str(DEFAULT_PROFILE_CHARS)},
            {"key": "prefetch_records", "description": "Records injected per turn by keyword retrieval", "default": str(DEFAULT_PREFETCH_RECORDS)},
            {"key": "max_record_chars", "description": "Body size cap per record before it must be shortened", "default": str(DEFAULT_MAX_RECORD_CHARS)},
            {"key": "keep_holographic", "description": "Keep fact_store/fact_feedback working via the holographic delegate", "default": "true", "choices": ["true", "false"]},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml

            from hermes_cli.config import read_user_config_raw  # raw: merged defaults must not be persisted

            existing = read_user_config_raw(config_path)
            existing.setdefault("plugins", {})["instinct-memory"] = values
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.dump(existing, handle, default_flow_style=False)
        except Exception as exc:
            logger.warning("instinct: save_config failed: %s", exc)

    def backup_paths(self) -> List[str]:
        # The vault lives under HERMES_HOME by default, so there is nothing outside it to list.
        try:
            from hermes_constants import get_hermes_home

            root = Path(_expand(str(self._cfg("vault_path", DEFAULT_VAULT)), str(get_hermes_home()))).resolve()
            return [] if str(root).startswith(str(Path(get_hermes_home()).resolve())) else [str(root)]
        except Exception:
            return []
