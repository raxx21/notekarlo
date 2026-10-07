"""The live notes document: items grouped by section, edited through small ops from Claude or the user."""

import time

SECTIONS = ["feedback", "actions", "decisions", "points", "questions"]
SECTION_TITLES = {
    "feedback": "Feedback",
    "actions": "Action Items / To-Do",
    "decisions": "Decisions",
    "points": "Key Points",
    "questions": "Open Questions / Follow-ups",
}
PREFIX = {"feedback": "f", "actions": "a", "decisions": "d", "points": "p", "questions": "q"}


class NotesState:
    def __init__(self):
        self.items: list[dict] = []
        self.counters = {s: 0 for s in SECTIONS}
        self.summary = ""
        self.topic = ""
        self.version = 0

    # ---- mutations -------------------------------------------------------
    def _new_id(self, section: str) -> str:
        self.counters[section] += 1
        return f"{PREFIX[section]}{self.counters[section]}"

    def get(self, item_id: str):
        return next((i for i in self.items if i["id"] == item_id), None)

    def add(self, section: str, text: str, owner="", due="", important=False, by_user=False) -> dict:
        if section not in SECTIONS:
            section = "points"
        now = time.time()
        item = {
            "id": self._new_id(section),
            "section": section,
            "text": text.strip(),
            "owner": (owner or "").strip(),
            "due": (due or "").strip(),
            "important": bool(important),
            "by_user": by_user,
            "created": now,
            "updated": now,
        }
        self.items.append(item)
        self.version += 1
        return item

    def apply_ops(self, ops: list[dict]) -> list[str]:
        """Apply Claude's edits. Returns ids that changed (for highlight in the UI)."""
        changed = []
        for op in ops or []:
            kind = op.get("op")
            if kind == "add":
                text = (op.get("text") or "").strip()
                if not text or self._is_duplicate(op.get("section"), text):
                    continue
                item = self.add(op.get("section") or "points", text, op.get("owner"), op.get("due"), op.get("important"))
                changed.append(item["id"])
            elif kind == "update":
                item = self.get(op.get("id") or "")
                if not item or item.get("by_user"):
                    continue
                for key in ("text", "owner", "due"):
                    if op.get(key):
                        item[key] = op[key].strip()
                if "important" in op and op["important"] is not None:
                    item["important"] = bool(op["important"]) or item["important"]
                item["updated"] = time.time()
                changed.append(item["id"])
            elif kind == "remove":
                item = self.get(op.get("id") or "")
                if item and not item.get("by_user"):
                    self.items.remove(item)
                    changed.append(item["id"])
        if changed:
            self.version += 1
        return changed

    def _is_duplicate(self, section, text: str) -> bool:
        norm = text.lower().strip(" .")
        return any(i["section"] == section and i["text"].lower().strip(" .") == norm for i in self.items)

    def edit_by_user(self, item_id: str, fields: dict) -> dict | None:
        item = self.get(item_id)
        if not item:
            return None
        for key in ("text", "owner", "due", "section"):
            if key in fields and fields[key] is not None:
                item[key] = str(fields[key]).strip()
        if "important" in fields:
            item["important"] = bool(fields["important"])
        if any(k in fields for k in ("text", "owner", "due", "section")):
            item["by_user"] = True
        item["updated"] = time.time()
        self.version += 1
        return item

    def delete(self, item_id: str) -> bool:
        item = self.get(item_id)
        if not item:
            return False
        self.items.remove(item)
        self.version += 1
        return True

    # ---- views -----------------------------------------------------------
    def for_prompt(self, me: str) -> str:
        lines = []
        for section in SECTIONS:
            items = [i for i in self.items if i["section"] == section]
            lines.append(f"{section}:")
            if not items:
                lines.append("  (empty)")
            for i in items:
                extra = []
                if i["owner"]:
                    extra.append(f"owner: {i['owner']}")
                if i["due"]:
                    extra.append(f"due: {i['due']}")
                if i["important"]:
                    extra.append("important")
                if i.get("by_user"):
                    extra.append(f"edited by {me}")
                suffix = f" ({'; '.join(extra)})" if extra else ""
                lines.append(f"  [{i['id']}] {i['text']}{suffix}")
        lines.append(f"summary: {self.summary or '(none yet)'}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "items": self.items,
            "counters": self.counters,
            "summary": self.summary,
            "topic": self.topic,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NotesState":
        state = cls()
        state.items = data.get("items", [])
        state.counters.update(data.get("counters", {}))
        state.summary = data.get("summary", "")
        state.topic = data.get("topic", "")
        state.version = data.get("version", 0)
        return state

    def to_markdown(self, title: str, date: str) -> str:
        out = [f"# {title} — Live Notes", f"_{date}_", ""]
        if self.summary:
            out += ["## Summary", self.summary, ""]
        for section in SECTIONS:
            items = [i for i in self.items if i["section"] == section]
            if not items:
                continue
            out.append(f"## {SECTION_TITLES[section]}")
            for i in items:
                meta = []
                if i["owner"]:
                    meta.append(f"Owner: {i['owner']}")
                if i["due"]:
                    meta.append(f"Deadline: {i['due']}")
                star = " **(Important)**" if i["important"] else ""
                tail = f" — {', '.join(meta)}" if meta else ""
                out.append(f"- {i['text']}{tail}{star}")
            out.append("")
        return "\n".join(out)
