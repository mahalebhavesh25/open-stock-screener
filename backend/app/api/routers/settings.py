from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.core.config import CFG, Config
from app.core.database import db
from app.engine.broker import GrowwBroker
from app.engine.llm_overlay import GeminiAnalyst
from app.engine.telegram import TelegramNotifier

router = APIRouter(prefix="/api/settings", tags=["settings"])
LOG = logging.getLogger("api.settings")


@router.get("/")
def get_settings() -> Dict[str, Any]:
    # Start with live CFG dictionary
    res = CFG.to_dict()
    # Merge persisted settings from SQLite if available
    saved_cfg = db.get_setting("engine_config")
    if saved_cfg and isinstance(saved_cfg, dict):
        res.update(saved_cfg)
        # Re-apply aliases so both naming formats are present
        temp = Config.from_dict(res)
        res = temp.to_dict()
    return res


def _apply_and_save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    global CFG
    try:
        current = CFG.to_dict()
        saved = db.get_setting("engine_config")
        if saved and isinstance(saved, dict):
            current.update(saved)
        current.update(updates)

        new_cfg = Config.from_dict(current)
        new_cfg.validate()

        # Update in-memory CFG
        for k, v in new_cfg.to_dict().items():
            if hasattr(CFG, k):
                setattr(CFG, k, v)

        # Persist complete dictionary to SQLite
        persisted = new_cfg.to_dict()
        db.set_setting("engine_config", persisted)
        from app.engine.screener import invalidate_panel_cache
        invalidate_panel_cache()
        LOG.info("Configuration updated and saved to SQLite app_settings")
        return {"status": "success", "config": persisted}
    except Exception as e:
        LOG.error("Failed to update settings: %s", e)
        raise HTTPException(status_code=400, detail=f"Configuration validation failed: {e}")


@router.post("/")
def save_settings_post(updates: Dict[str, Any]) -> Dict[str, Any]:
    return _apply_and_save_settings(updates)


@router.put("/")
def save_settings_put(updates: Dict[str, Any]) -> Dict[str, Any]:
    return _apply_and_save_settings(updates)


@router.post("/reset")
def reset_settings() -> Dict[str, Any]:
    global CFG
    default_cfg = Config()
    for k, v in default_cfg.to_dict().items():
        if hasattr(CFG, k):
            setattr(CFG, k, v)
    persisted = default_cfg.to_dict()
    db.set_setting("engine_config", persisted)
    LOG.info("Configuration reset to defaults and saved to SQLite")
    return {"status": "reset", "config": persisted}


@router.post("/test-groww")
def test_groww_connection(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        test_cfg = Config.from_dict(CFG.to_dict())
        if data:
            if data.get("access_token"):
                test_cfg.groww_access_token = data["access_token"]
            elif data.get("groww_access_token"):
                test_cfg.groww_access_token = data["groww_access_token"]
            if data.get("api_key"):
                test_cfg.groww_api_key = data["api_key"]
            if data.get("secret_key"):
                test_cfg.groww_api_secret = data["secret_key"]

        broker = GrowwBroker(test_cfg)
        broker.connect()
        q = broker.quote("TCS")
        return {
            "success": True,
            "ok": True,
            "message": "Successfully authenticated with Groww API",
            "sample_quote": q
        }
    except Exception as e:
        return {"success": False, "ok": False, "message": str(e), "error": str(e)}


@router.post("/test-gemini")
def test_gemini_connection(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        test_cfg = Config.from_dict(CFG.to_dict())
        if data:
            if data.get("api_key"):
                test_cfg.gemini_api_key = data["api_key"]
            if data.get("model"):
                test_cfg.gemini_model = data["model"]
        analyst = GeminiAnalyst(test_cfg)
        if not analyst.enabled:
            return {"success": False, "ok": False, "message": "Gemini API key is missing or not enabled"}
        reply = analyst._generate("Respond with: OK", temperature=0.1)
        return {"success": True, "ok": True, "message": f"Gemini connection active: {reply.strip()}"}
    except Exception as e:
        return {"success": False, "ok": False, "message": str(e), "error": str(e)}


@router.post("/test-telegram")
def test_telegram_connection(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        test_cfg = Config.from_dict(CFG.to_dict())
        if data:
            if data.get("bot_token"):
                test_cfg.telegram_token = data["bot_token"]
            if data.get("chat_id"):
                test_cfg.telegram_chat_id = data["chat_id"]
        notifier = TelegramNotifier(test_cfg)
        res = notifier.test_connection()
        return {
            "success": res.get("ok", False),
            "ok": res.get("ok", False),
            "message": res.get("message", res.get("error", "Telegram test finished"))
        }
    except Exception as e:
        return {"success": False, "ok": False, "message": str(e), "error": str(e)}
