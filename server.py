#!/usr/bin/env python3
"""
WholesaleHunter v2 — API Server
Serves the dashboard and exposes API endpoints to control the agent.
Launch with: python server.py
Dashboard opens at: http://localhost:8000
"""

import os
import sys
import json
import time
import threading
import logging
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from rich.console import Console
from rich.logging import RichHandler

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("wholesalehunter.server")
console = Console()

# ═══════════════════════════════════════════════════════════════
# AGENT STATE
# ═══════════════════════════════════════════════════════════════
agent_state = {
    "status": "idle",           # idle, running, stopped
    "current_step": None,       # which step is running
    "pipeline_running": False,
    "agent_loop_running": False, # continuous loop mode
    "cycle": 0,                 # current cycle number
    "loop_interval": 3600,      # seconds between cycles (1 hour)
    "last_run": None,
    "started_at": None,
    "log": [],                  # recent log entries
    "chat_history": [],         # conversation history for AI chat
    "stats": {
        "leads_scraped": 0,
        "leads_qualified": 0,
        "leads_stored": 0,
        "emails_sent": 0,
        "forms_filled": 0,
        "followups_sent": 0,
    },
    "errors": [],
}

MAX_LOG = 200  # keep last N log entries

def add_log(msg, level="info", category="system", data=None):
    """Add a structured log entry to the agent state."""
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "msg": msg,
        "level": level,
        "category": category,
        "data": data,
    }
    agent_state["log"].insert(0, entry)
    if len(agent_state["log"]) > MAX_LOG:
        agent_state["log"] = agent_state["log"][:MAX_LOG]
    logger.info(msg)


# ═══════════════════════════════════════════════════════════════
# PIPELINE RUNNER (runs in background thread)
# ═══════════════════════════════════════════════════════════════

def run_step_thread(step_name):
    """Run a single pipeline step in a background thread."""
    agent_state["status"] = "running"
    agent_state["current_step"] = step_name
    add_log(f"▶ Starting: {step_name}")

    try:
        if step_name == "scrape":
            from modules.scraper import run_daily_scrape
            leads = run_daily_scrape()
            agent_state["stats"]["leads_scraped"] = len(leads)
            agent_state["_temp_leads"] = leads
            add_log(f"✓ Scraped {len(leads)} leads (already inserted into Supabase per-country)")

        elif step_name == "qualify":
            raw = agent_state.get("_temp_leads", [])
            if not raw:
                add_log("⚠ No leads to qualify — run scrape first", "warning")
            else:
                try:
                    from modules.qualifier import qualify_leads
                    qualified = qualify_leads(raw, use_ai=True)
                    agent_state["stats"]["leads_qualified"] = len(qualified)
                    add_log(f"✓ Qualified {len(qualified)} leads")
                except Exception as qe:
                    add_log(f"⚠ Qualifier unavailable ({qe}) — leads already stored", "warning")

        elif step_name == "store":
            from modules.database import get_total_leads
            total = get_total_leads()
            add_log(f"✓ DB has {total} total leads")

        elif step_name == "email":
            from modules.database import get_leads_for_email
            from modules.emailer import send_initial_emails
            leads = get_leads_for_email(limit=1000)
            if not leads:
                add_log("⚠ No leads pending email", "warning")
            else:
                sent = send_initial_emails(leads)
                agent_state["stats"]["emails_sent"] = sent
                add_log(f"✓ Sent {sent} initial emails")

        elif step_name == "forms":
            from modules.form_outreach import run_form_outreach
            result = run_form_outreach(batch_size=100)
            agent_state["stats"]["forms_filled"] = result.get("success", 0)
            add_log(f"✓ Forms: {result.get('success',0)} success, {result.get('no_form',0)} no form, {result.get('failed',0)} failed")

        elif step_name == "followup":
            from modules.database import get_followup_due
            from modules.emailer import send_followup_emails
            leads = get_followup_due()
            if not leads:
                add_log("⚠ No follow-ups due today", "warning")
            else:
                sent = send_followup_emails(leads)
                agent_state["stats"]["followups_sent"] = sent
                add_log(f"✓ Sent {sent} follow-up emails")

        elif step_name == "report":
            from modules.intelligence import generate_weekly_report, format_report_text
            report = generate_weekly_report()
            text = format_report_text(report)
            add_log(f"✓ Intelligence report generated")

        else:
            add_log(f"Unknown step: {step_name}", "error")

    except Exception as e:
        add_log(f"✗ Error in {step_name}: {str(e)}", "error")
        agent_state["errors"].append({"step": step_name, "error": str(e), "time": datetime.now(timezone.utc).isoformat()})

    agent_state["status"] = "idle"
    agent_state["current_step"] = None
    agent_state["last_run"] = datetime.now(timezone.utc).isoformat()


def run_full_pipeline_thread():
    """Run the complete pipeline in sequence."""
    agent_state["pipeline_running"] = True
    agent_state["status"] = "running"
    add_log("🚀 Starting full pipeline...")

    steps = ["scrape", "qualify", "store", "email", "forms", "followup"]
    for step in steps:
        if not agent_state["pipeline_running"]:
            add_log("⏹ Pipeline stopped by user")
            break
        agent_state["current_step"] = step
        add_log(f"▶ Pipeline step: {step}")
        run_step_thread.__wrapped__(step) if hasattr(run_step_thread, '__wrapped__') else _run_step_sync(step)

    # Weekly report on Sundays
    if datetime.now(timezone.utc).strftime("%A").lower() == "sunday":
        add_log("▶ Sunday — generating weekly report")
        _run_step_sync("report")

    agent_state["pipeline_running"] = False
    agent_state["status"] = "idle"
    agent_state["current_step"] = None
    agent_state["last_run"] = datetime.now(timezone.utc).isoformat()
    add_log("✅ Full pipeline complete!")


def _run_step_sync(step_name):
    """Run a step synchronously (used within the pipeline thread)."""
    try:
        if step_name == "scrape":
            from modules.scraper import run_daily_scrape
            leads = run_daily_scrape()
            agent_state["stats"]["leads_scraped"] = len(leads)
            agent_state["_temp_leads"] = leads
            add_log(f"✓ Scraped {len(leads)} leads (inserted into Supabase per-country)")
        elif step_name == "qualify":
            raw = agent_state.get("_temp_leads", [])
            if raw:
                try:
                    from modules.qualifier import qualify_leads
                    qualified = qualify_leads(raw, use_ai=True)
                    agent_state["stats"]["leads_qualified"] = len(qualified)
                except Exception as qe:
                    add_log(f"⚠ Qualifier unavailable ({qe}) — leads already stored unscored", "warning")
                    agent_state["stats"]["leads_qualified"] = len(raw)
            else:
                add_log("⚠ No leads to qualify")
        elif step_name == "store":
            # Leads are now stored in the scrape step — this is just a status check
            from modules.database import get_total_leads
            total = get_total_leads()
            add_log(f"✓ DB has {total} total leads")
        elif step_name == "email":
            from modules.database import get_leads_for_email
            from modules.emailer import send_initial_emails
            leads = get_leads_for_email(1000)
            sent = send_initial_emails(leads) if leads else 0
            agent_state["stats"]["emails_sent"] = sent
            add_log(f"✓ Sent {sent} emails")
        elif step_name == "forms":
            from modules.form_outreach import run_form_outreach
            result = run_form_outreach(batch_size=100)
            agent_state["stats"]["forms_filled"] = result.get("success", 0)
            add_log(f"✓ Forms: {result.get('success',0)} success, {result.get('failed',0)} failed")
        elif step_name == "followup":
            from modules.database import get_followup_due
            from modules.emailer import send_followup_emails
            leads = get_followup_due()
            sent = send_followup_emails(leads) if leads else 0
            agent_state["stats"]["followups_sent"] = sent
            add_log(f"✓ Sent {sent} follow-ups")
        elif step_name == "report":
            from modules.intelligence import generate_weekly_report
            generate_weekly_report()
            add_log(f"✓ Report generated")
    except Exception as e:
        add_log(f"✗ {step_name} error: {e}", "error")


# ═══════════════════════════════════════════════════════════════
# CONTINUOUS AGENT LOOP (runs forever until stopped)
# ═══════════════════════════════════════════════════════════════

def agent_loop_thread():
    """
    Run all workers in parallel:
    - Scrape Worker: scrapes leads every cycle (runs scrape step, then waits)
    - Email Worker: polls DB for New leads, sends emails continuously
    - Followup Worker: polls DB for due follow-ups continuously

    All 3 run simultaneously until agent is stopped.
    """
    from modules.email_queue import start_email_workers, stop_email_workers, get_worker_status
    from modules.reply_tracker import start_reply_tracker, stop_reply_tracker

    agent_state["agent_loop_running"] = True
    agent_state["pipeline_running"] = True
    agent_state["status"] = "running"
    agent_state["started_at"] = datetime.now(timezone.utc).isoformat()
    agent_state["cycle"] = 0
    add_log("Agent started — Scraper + Emailer + Followup + Reply Tracker", category="system")

    # Start email + followup workers (they run independently)
    start_email_workers()
    start_reply_tracker()
    add_log("Email worker: polling DB for new leads", category="email")
    add_log("Reply tracker: checking inbox every 2 min", category="email")

    # Scrape worker loop (this thread handles scraping)
    while agent_state["agent_loop_running"]:
        agent_state["cycle"] += 1
        cycle = agent_state["cycle"]
        add_log(f"🔄 === Scrape cycle {cycle} ===")

        # Run scrape step
        agent_state["current_step"] = "scrape"
        agent_state["status"] = "running"
        _run_step_sync("scrape")

        # Update worker status for dashboard
        ws = get_worker_status()
        agent_state["worker_status"] = ws

        agent_state["current_step"] = None
        agent_state["last_run"] = datetime.now(timezone.utc).isoformat()

        if not agent_state["agent_loop_running"]:
            break

        add_log(f"✅ Scrape cycle {cycle} done — emailer + followup running in background")
        add_log(f"⏳ Next scrape in {agent_state['loop_interval']}s")

        # Wait for interval, checking every 5s
        waited = 0
        while waited < agent_state["loop_interval"] and agent_state["agent_loop_running"]:
            time.sleep(5)
            waited += 5
            agent_state["status"] = "waiting"
            # Update worker status periodically
            agent_state["worker_status"] = get_worker_status()

    # Stop all workers
    stop_email_workers()
    stop_reply_tracker()
    agent_state["agent_loop_running"] = False
    agent_state["pipeline_running"] = False
    agent_state["status"] = "idle"
    agent_state["current_step"] = None
    agent_state["worker_status"] = get_worker_status()
    add_log("All workers stopped", category="system")


# ═══════════════════════════════════════════════════════════════
# AI CHAT (uses Claude to answer questions about the agent)
# ═══════════════════════════════════════════════════════════════

def _get_db_context() -> str:
    """Pull comprehensive database stats for AI context."""
    from modules.database import db
    if not db:
        return "DATABASE: Not connected"

    lines = []
    try:
        total = db.count("leads")
        emailed = db.count("leads", {"email_sent": "eq.true"})
        replied = db.count("leads", {"replied": "eq.true"})
        interested = db.count("leads", {"interested": "eq.true"})
        closed = db.count("leads", {"closed": "eq.true"})
        opened = db.count("leads", {"email_opened": "eq.true"})
        lines.append(f"TOTALS: {total} leads, {emailed} emailed, {opened} opened, {replied} replied, {interested} interested, {closed} closed")
    except:
        lines.append("TOTALS: unavailable")

    # Country breakdown
    try:
        all_leads = db.select("leads", columns="country", limit=5000)
        country_counts = {}
        for l in all_leads:
            c = l.get("country", "Unknown")
            country_counts[c] = country_counts.get(c, 0) + 1
        top_countries = sorted(country_counts.items(), key=lambda x: -x[1])[:15]
        lines.append("LEADS BY COUNTRY: " + ", ".join(f"{c}: {n}" for c, n in top_countries))
    except:
        pass

    # Lead type breakdown
    try:
        type_counts = {}
        for l in all_leads:
            t = l.get("lead_type", "other")
            type_counts[t] = type_counts.get(t, 0) + 1
        lines.append("LEADS BY TYPE: " + ", ".join(f"{t}: {n}" for t, n in sorted(type_counts.items(), key=lambda x: -x[1])))
    except:
        pass

    # Email tracking stats
    try:
        tracking = db.select("email_tracking_stats", limit=1)
        if tracking:
            t = tracking[0]
            lines.append(f"EMAIL TRACKING: {t.get('total_tracked',0)} tracked, {t.get('total_opened',0)} opened, {t.get('unique_opens',0)} unique opens, {t.get('open_rate',0)}% open rate")
    except:
        pass

    # Source tracker
    try:
        sources = db.select("source_tracker", columns="source,country,total_found,status", order="total_found.desc", limit=20)
        if sources:
            source_summary = {}
            for s in sources:
                src = s.get("source", "?")
                source_summary[src] = source_summary.get(src, 0) + (s.get("total_found", 0) or 0)
            lines.append("LEADS BY SOURCE: " + ", ".join(f"{s}: {n}" for s, n in sorted(source_summary.items(), key=lambda x: -x[1])))
    except:
        pass

    # Top scoring leads
    try:
        hot = db.select("leads", columns="company_name,country,score,contact_email,replied,interested",
                        filters={"score": "gte.70", "email_sent": "eq.true"},
                        order="score.desc", limit=10)
        if hot:
            lines.append("TOP SCORED EMAILED LEADS: " + "; ".join(
                f"{l['company_name']} ({l['country']}, score:{l['score']}, replied:{l.get('replied',False)})" for l in hot
            ))
    except:
        pass

    # Segment performance
    try:
        segments = db.select("segment_performance", order="close_rate.desc", limit=10)
        if segments:
            lines.append("SEGMENT PERFORMANCE: " + "; ".join(
                f"{s['segment_type']}/{s['segment_value']}: {s['total_leads']} leads, {s.get('replies',0)} replies, {s.get('closed',0)} closed, paused={s.get('is_paused',False)}"
                for s in segments
            ))
    except:
        pass

    return "\n".join(lines)


def handle_chat(user_message: str) -> str:
    """Process a user chat message and return an AI response."""

    stats = agent_state["stats"]
    recent_logs = agent_state["log"][:10]
    log_text = "\n".join(f"[{l['time']}] {l['msg']}" for l in recent_logs)

    # Get comprehensive DB data
    db_context = _get_db_context()

    context = f"""You are the WholesaleHunter v2 AI agent for Rozper, a wholesale VoIP carrier.
You have FULL access to the database and system state. Answer with specific data and numbers.
You can recommend actions: start/stop agent, pause countries, change targets, send emails, etc.
Be direct and data-driven. Use the database info below to answer accurately.

AGENT STATE:
- Status: {agent_state['status']} | Running: {agent_state['agent_loop_running']} | Step: {agent_state['current_step'] or 'none'}
- Cycle: {agent_state['cycle']} | Last run: {agent_state['last_run'] or 'never'}
- Session stats: scraped={stats['leads_scraped']}, qualified={stats['leads_qualified']}, emails={stats['emails_sent']}, forms={stats['forms_filled']}, followups={stats['followups_sent']}

DATABASE:
{db_context}

RECENT LOG:
{log_text}

ERRORS: {json.dumps(agent_state['errors'][-3:], default=str) if agent_state['errors'] else 'None'}

RULES:
- Always cite exact numbers from the database
- If asked about countries, leads, sources, emails — use the data above
- If asked to take action (pause country, start agent, send emails) — explain what you'd do and recommend the user confirm
- Keep responses concise (2-4 paragraphs), use bullet points for data
- If data is missing, say so honestly"""

    messages = []
    for entry in agent_state["chat_history"][-6:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_message})

    from modules.ai_client import ai_generate, is_ai_available

    if not is_ai_available():
        return _fallback_chat(user_message)

    try:
        full_prompt = "\n".join(
            [f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}" for m in messages]
        )
        reply = ai_generate(full_prompt, max_tokens=800, system=context)
        if not reply:
            return _fallback_chat(user_message)

        # Save to history
        agent_state["chat_history"].append({"role": "user", "content": user_message})
        agent_state["chat_history"].append({"role": "assistant", "content": reply})
        if len(agent_state["chat_history"]) > 30:
            agent_state["chat_history"] = agent_state["chat_history"][-20:]

        return reply

    except Exception as e:
        logger.error(f"Chat AI error: {e}")
        return _fallback_chat(user_message)


def _fallback_chat(msg: str) -> str:
    """Smart fallback when no API key is available."""
    lower = msg.lower()
    stats = agent_state["stats"]

    if "status" in lower or "how" in lower:
        status = "running (cycle " + str(agent_state["cycle"]) + ")" if agent_state["agent_loop_running"] else "idle"
        return (f"Agent is currently {status}.\n\n"
                f"Stats this session:\n"
                f"• Leads scraped: {stats['leads_scraped']}\n"
                f"• Emails sent: {stats['emails_sent']}\n"
                f"• Forms filled: {stats['forms_filled']}\n"
                f"• Follow-ups: {stats['followups_sent']}\n\n"
                f"For AI-powered analysis, add your Anthropic API key to .env")

    if "improve" in lower or "sales" in lower or "better" in lower:
        return ("Here are some tips to improve performance:\n\n"
                "1. Check the Intelligence tab for segment analysis — pause countries with 0 closes after 200+ leads\n"
                "2. Focus on VoIP Providers and UCaaS — they tend to convert best\n"
                "3. Use both email + form fill (dual channel has 2x reply rate)\n"
                "4. Make sure follow-up sequences are running — most replies come from email #2 or #3\n"
                "5. Review domain health — rotate any domain under 10% open rate\n\n"
                "For personalized AI analysis, add your Anthropic API key to .env")

    if "start" in lower or "run" in lower:
        if agent_state["agent_loop_running"]:
            return f"The agent is already running! Currently on cycle {agent_state['cycle']}. It will keep running until you click Stop."
        return "Click the green START AGENT button on the dashboard to begin. The agent will scrape leads, qualify them, send emails, fill forms, and send follow-ups in a continuous loop every hour."

    if "stop" in lower:
        if not agent_state["agent_loop_running"]:
            return "The agent is already stopped. Click START AGENT to begin a new session."
        return "Click the red STOP AGENT button to stop the agent. It will finish the current step and then stop gracefully."

    return (f"I'm here to help! I can answer questions about:\n\n"
            f"• Agent status and pipeline progress\n"
            f"• Performance analysis and improvement tips\n"
            f"• Lead quality and segment insights\n"
            f"• Email deliverability and domain health\n\n"
            f"For full AI-powered conversation, add your Anthropic API key to .env")


# ═══════════════════════════════════════════════════════════════
# DATABASE QUERIES FOR DASHBOARD
# ═══════════════════════════════════════════════════════════════

def get_dashboard_data():
    """Get all data the dashboard needs."""
    from modules.database import db, get_total_leads, get_today_stats, get_hot_leads
    try:
        total = get_total_leads()
        today = get_today_stats()
        hot = get_hot_leads()

        # Get all leads for the table (limited)
        leads = db.select("leads", order="created_at.desc", limit=500) if db else []

        # Email stats
        emailed = db.count("leads", {"email_sent": "eq.true"}) if db else 0
        forms = db.count("leads", {"form_filled": "eq.true"}) if db else 0
        replies = db.count("leads", {"replied": "eq.true"}) if db else 0
        interested = db.count("leads", {"interested": "eq.true"}) if db else 0
        closed = db.count("leads", {"closed": "eq.true"}) if db else 0

        # Revenue
        closed_leads = db.select("leads", columns="revenue_monthly", filters={"closed": "eq.true"}) if db else []
        mrr = sum(r.get("revenue_monthly", 0) or 0 for r in closed_leads)

        # Source tracker
        sources = db.select("source_tracker", order="last_scraped.desc", limit=100) if db else []

        # Segment performance
        segments = db.select("segment_performance", order="close_rate.desc") if db else []

        return {
            "connected": db is not None,
            "total_leads": total,
            "today": today,
            "hot_leads": hot[:10],
            "leads": leads,
            "stats": {
                "emailed": emailed,
                "forms_filled": forms,
                "replies": replies,
                "interested": interested,
                "closed": closed,
                "mrr": mrr,
            },
            "sources": sources,
            "segments": segments,
        }
    except Exception as e:
        logger.error(f"Dashboard data error: {e}")
        return {"connected": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# HTTP SERVER
# ═══════════════════════════════════════════════════════════════

class AgentHTTPHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler for the agent API + dashboard."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API Routes
        if path == "/api/status":
            self._json_response({
                "status": agent_state["status"],
                "current_step": agent_state["current_step"],
                "pipeline_running": agent_state["pipeline_running"],
                "agent_loop_running": agent_state["agent_loop_running"],
                "cycle": agent_state["cycle"],
                "started_at": agent_state["started_at"],
                "last_run": agent_state["last_run"],
                "stats": agent_state["stats"],
                "workers": agent_state.get("worker_status", {}),
            })

        elif path == "/api/logs":
            self._json_response({"logs": agent_state["log"][:50]})

        elif path == "/api/dashboard":
            data = get_dashboard_data()
            self._json_response(data)

        elif path == "/api/leads":
            params = parse_qs(parsed.query)
            from modules.database import db
            if db:
                filters = {}
                if params.get("country"):
                    filters["country"] = f"eq.{params['country'][0]}"
                if params.get("lead_type"):
                    filters["lead_type"] = f"eq.{params['lead_type'][0]}"
                if params.get("source"):
                    filters["source"] = f"eq.{params['source'][0]}"
                limit = int(params.get("limit", [100])[0])
                offset = int(params.get("offset", [0])[0])
                leads = db.select("leads", filters=filters, order="created_at.desc", limit=limit)
                total = db.count("leads", filters)
                self._json_response({"leads": leads, "total": total})
            else:
                self._json_response({"leads": [], "total": 0})

        elif path == "/api/hot-leads":
            from modules.database import get_hot_leads
            self._json_response({"leads": get_hot_leads()})

        elif path == "/api/sources":
            from modules.database import db
            sources = db.select("source_tracker", order="last_scraped.desc", limit=200) if db else []
            self._json_response({"sources": sources})

        elif path == "/api/segments":
            from modules.database import get_segment_performance
            all_segments = get_segment_performance()  # all types
            self._json_response({"segments": all_segments})

        elif path == "/api/errors":
            self._json_response({"errors": agent_state["errors"][-20:]})

        elif path == "/api/email-stats":
            from modules.database import db
            if db:
                total_sent = db.count("outreach_log", {"channel": "eq.email"})
                recorded = db.count("outreach_log", {"channel": "eq.email", "delivery_status": "eq.recorded"})
                sent = db.count("outreach_log", {"channel": "eq.email", "delivery_status": "eq.sent"})
                failed = db.count("outreach_log", {"channel": "eq.email", "delivery_status": "eq.failed"})
                self._json_response({
                    "total_outreach": total_sent,
                    "recorded": recorded,
                    "sent": sent,
                    "failed": failed,
                })
            else:
                self._json_response({"total_outreach": 0})

        elif path == "/api/warmup-status":
            from modules.email_warmup import get_warmup_status, get_total_remaining_capacity
            self._json_response({
                "domains": get_warmup_status(),
                "total_remaining": get_total_remaining_capacity(),
            })

        elif path == "/api/email-queue":
            from modules.email_queue import get_queue_size
            self._json_response({"queue_size": get_queue_size()})

        elif path == "/api/email-tracking":
            from modules.database import db
            if db:
                try:
                    stats_rows = db.select("email_tracking_stats", limit=1)
                    stats = stats_rows[0] if stats_rows else {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}
                    recent_opens = db.select("email_tracking",
                        filters={"opened": "eq.true"},
                        order="opened_at.desc",
                        limit=20)
                    self._json_response({"stats": stats, "recent_opens": recent_opens})
                except Exception as e:
                    self._json_response({"stats": {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}, "recent_opens": [], "error": str(e)})
            else:
                self._json_response({"stats": {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}, "recent_opens": []})

        elif path == "/api/domain-emails":
            from modules.database import db
            params = parse_qs(parsed.query)
            domain = params.get("domain", [""])[0]
            if db and domain:
                logs = db.select("outreach_log",
                    filters={"channel": "eq.email", "sending_domain": f"eq.{domain}"},
                    order="sent_at.desc",
                    limit=100)
                # Enrich with lead + tracking info
                for log_entry in logs:
                    if log_entry.get("lead_id"):
                        lead_rows = db.select("leads",
                            columns="company_name,company_domain,contact_email,email_opened,replied,score,country",
                            filters={"id": f"eq.{log_entry['lead_id']}"},
                            limit=1)
                        if lead_rows:
                            log_entry["lead"] = lead_rows[0]
                        # Get tracking data
                        tracking_rows = db.select("email_tracking",
                            columns="opened,open_count,opened_at",
                            filters={"lead_id": f"eq.{log_entry['lead_id']}", "sequence_stage": f"eq.{log_entry.get('sequence_stage',1)}"},
                            limit=1)
                        if tracking_rows:
                            log_entry["tracking"] = tracking_rows[0]
                # Summary stats
                total = len(logs)
                sent_count = sum(1 for l in logs if l.get("delivery_status") == "sent")
                opened_count = sum(1 for l in logs if l.get("tracking", {}).get("opened"))
                replied_count = sum(1 for l in logs if l.get("lead", {}).get("replied"))
                self._json_response({
                    "emails": logs,
                    "domain": domain,
                    "summary": {
                        "total": total,
                        "sent": sent_count,
                        "opened": opened_count,
                        "replied": replied_count,
                        "open_rate": round(opened_count / sent_count * 100, 1) if sent_count else 0,
                        "reply_rate": round(replied_count / sent_count * 100, 1) if sent_count else 0,
                    }
                })
            else:
                self._json_response({"emails": [], "domain": domain, "summary": {}})

        elif path == "/api/form-outreach/status":
            from modules.form_outreach import get_outreach_status
            self._json_response(get_outreach_status())

        elif path == "/api/form-outreach/results":
            from modules.form_outreach import get_dashboard_results
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", [50])[0])
            results = get_dashboard_results(limit=limit)
            self._json_response({"results": results})

        elif path == "/api/email-sequences":
            from modules.database import db
            params = parse_qs(parsed.query)
            stage_filter = params.get("stage", [""])[0]
            if db:
                filters = {"channel": "eq.email"}
                if stage_filter:
                    filters["sequence_stage"] = f"eq.{stage_filter}"
                logs = db.select("outreach_log",
                    filters=filters,
                    order="sent_at.desc",
                    limit=200)
                for log_entry in logs:
                    if log_entry.get("lead_id"):
                        lead_rows = db.select("leads",
                            columns="company_name,company_domain,contact_email,email_opened,replied",
                            filters={"id": f"eq.{log_entry['lead_id']}"},
                            limit=1)
                        if lead_rows:
                            log_entry["lead"] = lead_rows[0]
                self._json_response({"sequences": logs})
            else:
                self._json_response({"sequences": []})

        elif path == "/" or path == "/dashboard":
            # Serve the dashboard
            self.path = "/dashboard.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        else:
            # Serve static files (dashboard.html, etc.)
            return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        if path == "/api/run-step":
            step = body.get("step", "")
            if agent_state["status"] == "running":
                self._json_response({"error": "Agent is already running"}, 409)
                return
            if step not in ["scrape", "qualify", "store", "email", "forms", "followup", "report"]:
                self._json_response({"error": f"Invalid step: {step}"}, 400)
                return
            t = threading.Thread(target=run_step_thread, args=(step,), daemon=True)
            t.start()
            self._json_response({"status": "started", "step": step})

        elif path == "/api/run-pipeline":
            if agent_state["pipeline_running"]:
                self._json_response({"error": "Pipeline already running"}, 409)
                return
            t = threading.Thread(target=run_full_pipeline_thread, daemon=True)
            t.start()
            self._json_response({"status": "pipeline_started"})

        elif path == "/api/start-agent":
            # Start continuous loop mode
            if agent_state["agent_loop_running"]:
                self._json_response({"error": "Agent already running"}, 409)
                return
            interval = body.get("interval", 3600)
            agent_state["loop_interval"] = int(interval)
            t = threading.Thread(target=agent_loop_thread, daemon=True)
            t.start()
            self._json_response({"status": "agent_started", "mode": "continuous", "interval": interval})

        elif path == "/api/stop-agent":
            # Stop ALL workers immediately
            from modules.email_queue import stop_email_workers
            from modules.reply_tracker import stop_reply_tracker
            stop_email_workers()
            stop_reply_tracker()
            agent_state["agent_loop_running"] = False
            agent_state["pipeline_running"] = False
            agent_state["status"] = "idle"
            add_log("Agent stopped — all workers halted", category="system")
            self._json_response({"status": "agent_stopped"})

        elif path == "/api/form-outreach/start":
            from modules.form_outreach import start_form_outreach_background
            batch_size = body.get("batch_size", 10)
            result = start_form_outreach_background(batch_size=int(batch_size))
            add_log(f"▶ Form outreach started (batch={batch_size})", category="form")
            self._json_response(result)

        elif path == "/api/form-outreach/stop":
            from modules.form_outreach import stop_form_outreach
            result = stop_form_outreach()
            add_log("⏹ Form outreach stop requested", category="form")
            self._json_response(result)

        elif path == "/api/form-outreach/clean-urls":
            from modules.form_outreach import clean_all_pending_urls
            result = clean_all_pending_urls()
            add_log(f"🧹 Cleaned {result.get('cleaned', 0)}/{result.get('total', 0)} URLs", category="form")
            self._json_response(result)

        elif path == "/api/chat":
            # AI chat with the agent
            message = body.get("message", "").strip()
            if not message:
                self._json_response({"error": "Empty message"}, 400)
                return
            try:
                reply = handle_chat(message)
                self._json_response({"reply": reply})
            except Exception as e:
                logger.error(f"Chat error: {e}")
                self._json_response({"reply": f"Sorry, I encountered an error: {str(e)}"})

        elif path == "/api/stop":
            from modules.email_queue import stop_email_workers
            from modules.reply_tracker import stop_reply_tracker
            stop_email_workers()
            stop_reply_tracker()
            agent_state["agent_loop_running"] = False
            agent_state["pipeline_running"] = False
            agent_state["status"] = "idle"
            add_log("All workers stopped", category="system")
            self._json_response({"status": "stop_requested"})

        elif path == "/api/connect":
            # Update Supabase connection
            url = body.get("supabase_url", "")
            key = body.get("supabase_key", "")
            if url and key:
                from modules import database as dbmod
                dbmod.db = dbmod.SupabaseREST(url, key)
                # Update config
                os.environ["SUPABASE_URL"] = url
                os.environ["SUPABASE_KEY"] = key
                # Test connection
                try:
                    test = dbmod.db.count("leads")
                    add_log(f"✓ Connected to Supabase ({test} leads found)")
                    self._json_response({"status": "connected", "leads_count": test})
                except Exception as e:
                    self._json_response({"error": str(e)}, 500)
            else:
                self._json_response({"error": "Missing url or key"}, 400)

        elif path == "/api/update-lead":
            from modules.database import update_lead
            lead_id = body.get("id")
            updates = body.get("updates", {})
            if lead_id and updates:
                result = update_lead(lead_id, updates)
                self._json_response({"status": "updated", "lead": result})
            else:
                self._json_response({"error": "Missing id or updates"}, 400)

        elif path == "/api/settings":
            # Save settings to config at runtime
            try:
                import config
                if body.get("leadTarget"):
                    config.DAILY_LEAD_TARGET = int(body["leadTarget"])
                if body.get("minScore"):
                    config.SCORE_THRESHOLDS["min_qualify"] = int(body["minScore"])
                if body.get("emailsPerDomain"):
                    config.EMAILS_PER_DOMAIN = int(body["emailsPerDomain"])
                if body.get("countries"):
                    config.TARGET_COUNTRIES = [c.strip() for c in body["countries"].split(",") if c.strip()]
                if body.get("domains"):
                    config.SENDING_DOMAINS = [d.strip() for d in body["domains"].split("\n") if d.strip()]
                if body.get("pauseCountry"):
                    config.AUTO_EXCLUSION["country_pause_after_leads"] = int(body["pauseCountry"])
                if body.get("minClose"):
                    config.AUTO_EXCLUSION["lead_type_min_close_rate"] = float(body["minClose"]) / 100
                if body.get("minReply"):
                    config.AUTO_EXCLUSION["source_min_reply_rate"] = float(body["minReply"]) / 100
                add_log("Settings updated via dashboard", category="system")
                self._json_response({"status": "saved"})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/export-csv":
            from modules.database import db
            if db:
                leads = db.select("leads", order="created_at.desc", limit=10000)
                self._json_response({"leads": leads})
            else:
                self._json_response({"leads": []})

        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        """Send a JSON response."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default access logs (we use our own logging)."""
        pass


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    PORT = int(os.getenv("PORT", 8000))

    # Register structured log callback for modules
    from modules.events import set_log_callback
    set_log_callback(add_log)

    print()
    print("  WholesaleHunter v2 - Command Center")
    print(f"  Dashboard:  http://localhost:{PORT}")
    print(f"  API:        http://localhost:{PORT}/api/status")
    print()
    print("  The dashboard controls the agent.")
    print("  Press Ctrl+C to stop the server.")
    print()

    # Open browser automatically (skip if NOBROWSER env is set)
    if not os.getenv("NOBROWSER"):
        webbrowser.open(f"http://localhost:{PORT}")

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", PORT), AgentHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
