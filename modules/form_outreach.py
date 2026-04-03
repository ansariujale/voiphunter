"""
WholesaleHunter v2 — Form Outreach Orchestrator
Fetches new leads from Supabase, fills their contact forms, logs results.
Standalone module with state tracking for dashboard polling.
"""

import logging
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

from modules.database import (
    get_leads_for_form_outreach,
    update_form_status,
    get_form_outreach_results,
    update_lead,
)
from modules.form_filler import run_form_filling

logger = logging.getLogger("wholesalehunter.form_outreach")


# ═══════════════════════════════════════════════════════════════
# OUTREACH STATE (polled by dashboard)
# ═══════════════════════════════════════════════════════════════

outreach_state = {
    "running": False,
    "stop_requested": False,
    "batch_size": 10,
    "total_processed": 0,
    "success": 0,
    "failed": 0,
    "no_form": 0,
    "current_lead": None,
    "started_at": None,
    "finished_at": None,
    "results": [],  # last N results for dashboard table
    "error": None,
}

_worker_thread: Optional[threading.Thread] = None


def get_outreach_status() -> dict:
    """Return current outreach state for dashboard polling."""
    return {
        "running": outreach_state["running"],
        "batch_size": outreach_state["batch_size"],
        "total_processed": outreach_state["total_processed"],
        "success": outreach_state["success"],
        "failed": outreach_state["failed"],
        "no_form": outreach_state["no_form"],
        "current_lead": outreach_state["current_lead"],
        "started_at": outreach_state["started_at"],
        "finished_at": outreach_state["finished_at"],
        "error": outreach_state["error"],
    }


# ═══════════════════════════════════════════════════════════════
# CORE ORCHESTRATION
# ═══════════════════════════════════════════════════════════════

async def run_form_outreach_async(batch_size: int = 10) -> dict:
    """
    Main form outreach pipeline:
    1. Fetch pending leads
    2. Mark as processing
    3. Fill forms via Playwright
    4. Update status in Supabase
    5. Return summary
    """
    outreach_state["running"] = True
    outreach_state["stop_requested"] = False
    outreach_state["batch_size"] = batch_size
    outreach_state["total_processed"] = 0
    outreach_state["success"] = 0
    outreach_state["failed"] = 0
    outreach_state["no_form"] = 0
    outreach_state["current_lead"] = None
    outreach_state["started_at"] = datetime.now(timezone.utc).isoformat()
    outreach_state["finished_at"] = None
    outreach_state["results"] = []
    outreach_state["error"] = None

    try:
        # Step 1: Fetch pending leads
        logger.info(f"Fetching up to {batch_size} leads for form outreach...")
        leads = get_leads_for_form_outreach(limit=batch_size)

        if not leads:
            logger.info("No pending leads found for form outreach")
            outreach_state["running"] = False
            outreach_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            return {"total": 0, "success": 0, "failed": 0, "no_form": 0, "message": "No pending leads"}

        logger.info(f"Found {len(leads)} leads for form outreach")

        # Step 2: Mark all as processing
        for lead in leads:
            if outreach_state["stop_requested"]:
                break
            update_form_status(lead["id"], "processing")

        # Step 3: Run form filling
        if not outreach_state["stop_requested"]:
            stats = await run_form_filling(leads, max_concurrent=3)

            outreach_state["total_processed"] = stats.get("total", 0)
            outreach_state["success"] = stats.get("success", 0)
            outreach_state["failed"] = stats.get("failed", 0)
            outreach_state["no_form"] = stats.get("no_form", 0)

            # Store results for dashboard
            outreach_state["results"] = stats.get("results", [])

        # Step 4: Load final results from DB
        outreach_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        outreach_state["running"] = False
        outreach_state["current_lead"] = None

        summary = {
            "total": outreach_state["total_processed"],
            "success": outreach_state["success"],
            "failed": outreach_state["failed"],
            "no_form": outreach_state["no_form"],
        }
        logger.info(f"Form outreach complete: {summary}")
        return summary

    except Exception as e:
        logger.error(f"Form outreach error: {e}")
        outreach_state["error"] = str(e)[:300]
        outreach_state["running"] = False
        outreach_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        return {"error": str(e)}


def run_form_outreach(batch_size: int = 10) -> dict:
    """Synchronous wrapper for form outreach."""
    return asyncio.run(run_form_outreach_async(batch_size))


# ═══════════════════════════════════════════════════════════════
# BACKGROUND THREAD CONTROL
# ═══════════════════════════════════════════════════════════════

def start_form_outreach_background(batch_size: int = 10) -> dict:
    """Start form outreach in a background thread. Returns immediately."""
    global _worker_thread

    if outreach_state["running"]:
        return {"status": "already_running", "message": "Form outreach is already running"}

    def worker():
        try:
            run_form_outreach(batch_size)
        except Exception as e:
            logger.error(f"Background form outreach error: {e}")
            outreach_state["error"] = str(e)[:300]
            outreach_state["running"] = False

    _worker_thread = threading.Thread(target=worker, daemon=True, name="form_outreach_worker")
    _worker_thread.start()

    return {"status": "started", "batch_size": batch_size, "message": f"Form outreach started for {batch_size} leads"}


def stop_form_outreach() -> dict:
    """Request stop of running form outreach."""
    if not outreach_state["running"]:
        return {"status": "not_running", "message": "Form outreach is not running"}

    outreach_state["stop_requested"] = True
    return {"status": "stop_requested", "message": "Stop requested — will finish current lead then stop"}


def get_dashboard_results(limit: int = 50) -> list[dict]:
    """Get form outreach results from DB for dashboard table."""
    return get_form_outreach_results(limit=limit)
