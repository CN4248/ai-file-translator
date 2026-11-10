from __future__ import annotations

import copy
import os

from .db import Database

DEFAULTS = {
    "ai": {
        "enabled": bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")),
        "base_url": "https://api.deepseek.com/v1",
        "api_key": os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
        "model": "deepseek-chat",
        "temperature": 0.2,
        "timeout": 120,
        "polish": True,
    },
    "default_target": "zh",
    "default_mode": "offline_first",
    "auto_resume": True,
}


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class SettingsService:
    def __init__(self, db: Database):
        self.db = db
        self._defaults = copy.deepcopy(DEFAULTS)
        stored_root = db.get_settings()
        stored = stored_root.get("app", stored_root) if isinstance(stored_root, dict) else {}
        self._data = _merge(self._defaults, stored)
        if not (stored.get("ai") or {}).get("api_key") and self._defaults["ai"]["api_key"]:
            self._data["ai"]["api_key"] = self._defaults["ai"]["api_key"]
        try:
            self._validate()
        except Exception:
            self._data = copy.deepcopy(self._defaults)

    def get(self) -> dict:
        return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        patch = patch or {}
        ai_patch = dict(patch.get("ai") or {})
        if "api_key" in ai_patch and (
            ai_patch["api_key"] in ("***", "********", None)
            or not str(ai_patch["api_key"]).strip()
        ):
            # The public settings response masks/omits the key; a blank value from
            # the UI means "keep the stored key", not "erase it".
            ai_patch.pop("api_key", None)
        if "ai" in patch:
            patch = dict(patch)
            patch["ai"] = ai_patch
        self._data = _merge(self._data, patch)
        self._validate()
        self.db.set_setting("app", self._data)
        return self.get()

    def _validate(self) -> None:
        ai = self._data.setdefault("ai", {})
        ai["temperature"] = max(0.0, min(2.0, float(ai.get("temperature", 0.2))))
        ai["timeout"] = max(5, min(600, int(ai.get("timeout", 120))))
        ai["base_url"] = str(ai.get("base_url") or "").strip()
        ai["model"] = str(ai.get("model") or "").strip()
        ai["api_key"] = str(ai.get("api_key") or "").strip()
        ai["enabled"] = bool(ai.get("enabled"))
        ai["polish"] = bool(ai.get("polish", True))
        from .languages import ensure_supported

        self._data["default_target"] = ensure_supported(self._data.get("default_target", "zh"))
        mode = self._data.get("default_mode", "offline_first")
        if mode not in ("offline_first", "ai_first", "offline_only"):
            mode = "offline_first"
        self._data["default_mode"] = mode

    def public(self) -> dict:
        data = self.get()
        api_key = data.get("ai", {}).get("api_key", "")
        data["ai"] = dict(data.get("ai", {}))
        data["ai"]["api_key"] = ""
        data["ai"]["api_key_set"] = bool(api_key)
        return data
