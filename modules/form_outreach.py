"""
WholesaleHunter v2 — Form Outreach Orchestrator
Fetches new leads from Supabase, CLEANS their URLs in the DB first,
then fills their contact forms and logs results.
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
    db,
)
from modules.form_filler import run_form_filling, clean_website_url

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
    "results": [],
    "error": None,
    "urls_cleaned": 0,
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
        "urls_cleaned": outreach_state["urls_cleaned"],
    }


# ═══════════════════════════════════════════════════════════════
# URL CLEANING — Updates DB directly before processing
# ═══════════════════════════════════════════════════════════════

def clean_lead_urls(leads: list[dict]) -> int:
    """
    Clean website URLs for a batch of leads.
    Strips all paths/endpoints/query params, keeps base domain only.
    Updates BOTH the in-memory lead dict AND the Supabase database.
    Returns count of URLs that were actually changed.
    """
    cleaned_count = 0
    for lead in leads:
        raw_url = lead.get("website_url", "")
        if not raw_url:
            continue

        cleaned = clean_website_url(raw_url)
        if cleaned and cleaned != raw_url:
            logger.info(f"Cleaning URL for {lead.get('company_name', '?')}: {raw_url} -> {cleaned}")
            # Update in-memory
            lead["website_url"] = cleaned
            # Update in database
            try:
                update_lead(lead["id"], {"website_url": cleaned})
            except Exception as e:
                logger.error(f"Failed to update URL in DB for {lead.get('id')}: {e}")
            cleaned_count += 1

    return cleaned_count


def clean_all_pending_urls() -> dict:
    """
    Clean ALL pending lead URLs in the database.
    Called on-demand via API endpoint.
    Returns summary of how many were cleaned.
    """
    if not db:
        return {"cleaned": 0, "total": 0, "error": "No database connection"}

    try:
        # Fetch all pending leads
        leads = db.select("leads", filters={
            "form_submission_status": "eq.pending",
        }, order="created_at.asc", limit=5000)

        if not leads:
            return {"cleaned": 0, "total": 0}

        cleaned = 0
        for lead in leads:
            raw_url = lead.get("website_url", "")
            if not raw_url:
                continue
            new_url = clean_website_url(raw_url)
            if new_url and new_url != raw_url:
                try:
                    update_lead(lead["id"], {"website_url": new_url})
                    cleaned += 1
                except Exception:
                    pass

        logger.info(f"Bulk URL clean: {cleaned}/{len(leads)} URLs updated in database")
        return {"cleaned": cleaned, "total": len(leads)}

    except Exception as e:
        logger.error(f"Bulk URL clean error: {e}")
        return {"cleaned": 0, "total": 0, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# CORE ORCHESTRATION
# ═══════════════════════════════════════════════════════════════

async def run_form_outreach_async(batch_size: int = 10) -> dict:
    """
    Main form outreach pipeline:
    1. Fetch pending leads
    2. CLEAN all URLs in DB (strip endpoints to base domain)
    3. Mark as processing
    4. Fill forms via Playwright
    5. Update status in Supabase
    6. Return summary
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
    outreach_state["urls_cleaned"] = 0

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

        # *** Step 2: CLEAN ALL URLs IN DATABASE before anything else ***
        logger.info("Step 2: Cleaning all website URLs (stripping endpoints to base domain)...")
        urls_cleaned = clean_lead_urls(leads)
        outreach_state["urls_cleaned"] = urls_cleaned
        logger.info(f"URL cleaning complete: {urls_cleaned}/{len(leads)} URLs were cleaned")

        # Log the cleaned URLs for verification
        for lead in leads:
            logger.info(f"  Lead: {lead.get('company_name', '?')} -> {lead.get('website_url', '?')}")

        # Step 3: Mark all as processing
        for lead in leads:
            if outreach_state["stop_requested"]:
                break
            update_form_status(lead["id"], "processing")

        # Step 4: Run form filling
        if not outreach_state["stop_requested"]:
            stats = await run_form_filling(leads, max_concurrent=3)

            outreach_state["total_processed"] = stats.get("total", 0)
            outreach_state["success"] = stats.get("success", 0)
            outreach_state["failed"] = stats.get("failed", 0)
            outreach_state["no_form"] = stats.get("no_form", 0)

            # Store results for dashboard
            outreach_state["results"] = stats.get("results", [])

        # Step 5: Finalize
        outreach_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        outreach_state["running"] = False
        outreach_state["current_lead"] = None

        summary = {
            "total": outreach_state["total_processed"],
            "success": outreach_state["success"],
            "failed": outreach_state["failed"],
            "no_form": outreach_state["no_form"],
            "urls_cleaned": urls_cleaned,
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
