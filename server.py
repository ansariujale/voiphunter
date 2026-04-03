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
    """Process a user chat message and return an AI response with rich data."""

    stats = agent_state["stats"]
    recent_logs = agent_state["log"][:15]
    log_text = "\n".join(f"[{l['time']}] {l['msg']}" for l in recent_logs)

    # Get comprehensive DB data
    db_context = _get_db_context()

    # Get additional computed metrics for deeper analysis
    extra_context = _get_deep_analytics()

    context = f"""You are the WholesaleHunter v2 AI command center for Rozper, a wholesale VoIP carrier.
You are a senior sales operations analyst with FULL access to the database, pipeline, and system state.

YOUR ROLE:
- Answer ANY question about the project with exact data and numbers
- Diagnose problems (low reply rates, poor conversion, domain issues) with root cause analysis
- Recommend specific, actionable improvements with expected impact
- Provide strategic insights on lead quality, segment performance, and outreach optimization
- Help the user understand what's working, what isn't, and why

AGENT STATE:
- Status: {agent_state['status']} | Running: {agent_state['agent_loop_running']} | Step: {agent_state['current_step'] or 'none'}
- Cycle: {agent_state['cycle']} | Last run: {agent_state['last_run'] or 'never'}
- Session stats: scraped={stats['leads_scraped']}, qualified={stats['leads_qualified']}, emails={stats['emails_sent']}, forms={stats['forms_filled']}, followups={stats['followups_sent']}

DATABASE OVERVIEW:
{db_context}

DEEP ANALYTICS:
{extra_context}

RECENT ACTIVITY LOG:
{log_text}

ERRORS: {json.dumps(agent_state['errors'][-5:], default=str) if agent_state['errors'] else 'None'}

RESPONSE FORMATTING RULES:
- Always cite **exact numbers** from the database — never guess
- Use **bold** for key metrics and action items
- Use bullet points (•) for lists of data
- Structure longer answers with clear sections
- When diagnosing problems, always provide: 1) Current metric 2) What's wrong 3) Why 4) Fix
- When asked "why" something is low/bad, give a specific root cause analysis with data
- For action recommendations, be specific: "Pause India (0 closes from 234 leads)" not "consider pausing underperformers"
- If the user asks about leads, reply rates, or performance — pull actual numbers and compare segments
- Keep responses focused and data-driven (3-5 paragraphs max)
- If data is missing or zero, say so honestly and explain what that means
- When recommending actions, explain the expected impact"""

    messages = []
    for entry in agent_state["chat_history"][-8:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_message})

    from modules.ai_client import ai_generate, is_ai_available

    if not is_ai_available():
        return _fallback_chat(user_message)

    try:
        full_prompt = "\n".join(
            [f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}" for m in messages]
        )
        reply = ai_generate(full_prompt, max_tokens=1200, system=context)
        if not reply:
            return _fallback_chat(user_message)

        # Save to history
        agent_state["chat_history"].append({"role": "user", "content": user_message})
        agent_state["chat_history"].append({"role": "assistant", "content": reply})
        if len(agent_state["chat_history"]) > 40:
            agent_state["chat_history"] = agent_state["chat_history"][-24:]

        return reply

    except Exception as e:
        logger.error(f"Chat AI error: {e}")
        return _fallback_chat(user_message)


def _get_deep_analytics() -> str:
    """Compute deeper analytics for AI context — reply rates, conversion, diagnostics."""
    lines = []
    try:
        from config import get_db
        db = get_db()
        if not db:
            return "Deep analytics unavailable — no DB connection"

        # Overall funnel metrics
        try:
            total = db.select("leads", columns="id", limit=1, count="exact")
            total_count = total[0].get("count", 0) if total else 0

            emailed = db.select("leads", columns="id", filters={"email_sent": "eq.true"}, limit=1, count="exact")
            emailed_count = emailed[0].get("count", 0) if emailed else 0

            replied_leads = db.select("leads", columns="id", filters={"replied": "eq.true"}, limit=1, count="exact")
            replied_count = replied_leads[0].get("count", 0) if replied_leads else 0

            closed_leads = db.select("leads", columns="id", filters={"closed": "eq.true"}, limit=1, count="exact")
            closed_count = closed_leads[0].get("count", 0) if closed_leads else 0

            reply_rate = round(replied_count / emailed_count * 100, 2) if emailed_count > 0 else 0
            close_rate = round(closed_count / emailed_count * 100, 2) if emailed_count > 0 else 0

            lines.append(f"FUNNEL: {total_count} total leads → {emailed_count} emailed → {replied_count} replied ({reply_rate}%) → {closed_count} closed ({close_rate}%)")
        except:
            pass

        # Reply rate by country
        try:
            all_leads = db.select("leads", columns="country,email_sent,replied,closed,score", limit=5000)
            if all_leads:
                country_stats = {}
                for l in all_leads:
                    c = l.get("country", "Unknown")
                    if c not in country_stats:
                        country_stats[c] = {"total": 0, "emailed": 0, "replied": 0, "closed": 0, "scores": []}
                    country_stats[c]["total"] += 1
                    if l.get("email_sent"): country_stats[c]["emailed"] += 1
                    if l.get("replied"): country_stats[c]["replied"] += 1
                    if l.get("closed"): country_stats[c]["closed"] += 1
                    if l.get("score"): country_stats[c]["scores"].append(l["score"])

                country_lines = []
                for c, s in sorted(country_stats.items(), key=lambda x: -x[1]["total"]):
                    rr = round(s["replied"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    cr = round(s["closed"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    avg_score = round(sum(s["scores"]) / len(s["scores"]), 1) if s["scores"] else 0
                    country_lines.append(f"{c}: {s['total']} leads, {s['emailed']} emailed, {rr}% reply, {cr}% close, avg_score={avg_score}")
                lines.append("COUNTRY PERFORMANCE:\n  " + "\n  ".join(country_lines[:12]))

                # Lead type performance
                type_stats = {}
                for l in all_leads:
                    t = l.get("lead_type", "other")
                    if t not in type_stats:
                        type_stats[t] = {"total": 0, "emailed": 0, "replied": 0, "closed": 0}
                    type_stats[t]["total"] += 1
                    if l.get("email_sent"): type_stats[t]["emailed"] += 1
                    if l.get("replied"): type_stats[t]["replied"] += 1
                    if l.get("closed"): type_stats[t]["closed"] += 1

                type_lines = []
                for t, s in sorted(type_stats.items(), key=lambda x: -x[1]["total"]):
                    rr = round(s["replied"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    type_lines.append(f"{t}: {s['total']} leads, {s['emailed']} emailed, {rr}% reply, {s['closed']} closed")
                lines.append("LEAD TYPE PERFORMANCE:\n  " + "\n  ".join(type_lines))
        except:
            pass

        # Domain health
        try:
            outreach = db.select("outreach_log", columns="sending_domain,delivery_status", limit=5000)
            if outreach:
                domain_stats = {}
                for o in outreach:
                    d = o.get("sending_domain", "unknown")
                    if d not in domain_stats:
                        domain_stats[d] = {"sent": 0, "failed": 0, "bounced": 0}
                    domain_stats[d]["sent"] += 1
                    if o.get("delivery_status") == "failed": domain_stats[d]["failed"] += 1
                    if o.get("delivery_status") == "bounced": domain_stats[d]["bounced"] += 1
                domain_lines = []
                for d, s in sorted(domain_stats.items(), key=lambda x: -x[1]["sent"]):
                    fail_rate = round((s["failed"] + s["bounced"]) / s["sent"] * 100, 1) if s["sent"] > 0 else 0
                    domain_lines.append(f"{d}: {s['sent']} sent, {s['failed']} failed, {s['bounced']} bounced ({fail_rate}% fail)")
                lines.append("DOMAIN SEND STATS:\n  " + "\n  ".join(domain_lines))
        except:
            pass

        # Paused segments
        try:
            paused = db.select("segment_performance", filters={"is_paused": "eq.true"}, limit=50)
            if paused:
                lines.append("PAUSED SEGMENTS: " + "; ".join(
                    f"{s['segment_type']}/{s['segment_value']} (reason: {s.get('pause_reason','unknown')})" for s in paused
                ))
            else:
                lines.append("PAUSED SEGMENTS: None currently paused")
        except:
            pass

    except Exception as e:
        lines.append(f"Analytics error: {str(e)}")

    return "\n".join(lines)


def _fallback_chat(msg: str) -> str:
    """Smart fallback with real DB data when no API key is available."""
    lower = msg.lower()
    stats = agent_state["stats"]

    # Try to get real DB numbers even without AI
    db_nums = {"total": 0, "emailed": 0, "replied": 0, "closed": 0, "reply_rate": "0"}
    try:
        from config import get_db
        db = get_db()
        if db:
            all_l = db.select("leads", columns="email_sent,replied,closed", limit=10000)
            if all_l:
                db_nums["total"] = len(all_l)
                db_nums["emailed"] = sum(1 for l in all_l if l.get("email_sent"))
                db_nums["replied"] = sum(1 for l in all_l if l.get("replied"))
                db_nums["closed"] = sum(1 for l in all_l if l.get("closed"))
                if db_nums["emailed"] > 0:
                    db_nums["reply_rate"] = str(round(db_nums["replied"] / db_nums["emailed"] * 100, 1))
    except:
        pass

    if "status" in lower or "report" in lower or ("how" in lower and "going" in lower):
        status = f"running (cycle {agent_state['cycle']})" if agent_state["agent_loop_running"] else "idle"
        return (f"**Agent Status: {status.upper()}**\n\n"
                f"**Database Totals:**\n"
                f"• Total leads: {db_nums['total']:,}\n"
                f"• Emails sent: {db_nums['emailed']:,}\n"
                f"• Replies: {db_nums['replied']:,} ({db_nums['reply_rate']}% reply rate)\n"
                f"• Closed: {db_nums['closed']:,}\n\n"
                f"**Session Stats:**\n"
                f"• Scraped: {stats['leads_scraped']} | Qualified: {stats['leads_qualified']}\n"
                f"• Emails: {stats['emails_sent']} | Forms: {stats['forms_filled']}\n"
                f"• Follow-ups: {stats['followups_sent']}\n\n"
                f"Add your Anthropic API key to .env for deeper AI-powered analysis.")

    if "reply" in lower and ("low" in lower or "why" in lower or "improve" in lower):
        return (f"**Reply Rate Analysis**\n\n"
                f"Current reply rate: **{db_nums['reply_rate']}%** ({db_nums['replied']} replies from {db_nums['emailed']} emails)\n\n"
                f"**Common causes for low reply rates:**\n"
                f"1. **Domain health** — Rotate domains below 10% open rate\n"
                f"2. **Lead quality** — Increase score threshold above 40\n"
                f"3. **Email content** — Check personalization scores on variants\n"
                f"4. **Follow-ups missing** — Most replies come from email #2 or #3\n"
                f"5. **Wrong segments** — Pause countries with 0 closes after 200+ leads\n\n"
                f"**Quick fixes:** Run follow-up sequences, use dual-channel (email+form for 2x reply rate), focus on VoIP Providers and UCaaS.\n\n"
                f"Add your API key for AI-powered root cause analysis with your actual segment data.")

    if "improve" in lower or "sales" in lower or "better" in lower:
        return (f"**Performance Improvement Recommendations**\n\n"
                f"Based on typical patterns ({db_nums['total']:,} leads, {db_nums['reply_rate']}% reply rate):\n\n"
                f"1. **Segment analysis** — Pause countries with 0 closes after 200+ leads\n"
                f"2. **Focus targeting** — VoIP Providers and UCaaS convert best\n"
                f"3. **Dual channel** — Email + form fill has 2x higher reply rate\n"
                f"4. **Follow-up cadence** — 4 emails over 14 days, most replies on #2-3\n"
                f"5. **Domain rotation** — Swap any domain under 10% open rate\n"
                f"6. **Lead scoring** — Raise threshold to 50+ for better quality\n\n"
                f"Check the Intelligence tab for real segment data.")

    if "start" in lower or "run" in lower:
        if agent_state["agent_loop_running"]:
            return f"**Agent is already running** on cycle {agent_state['cycle']}. It will keep processing until you stop it."
        return ("**Ready to start.** The agent will run: Scrape → Qualify → Email → Forms → Follow-up → Report in a loop every hour.\n\n"
                "Use the /start command or click START AGENT on the dashboard.")

    if "stop" in lower:
        if not agent_state["agent_loop_running"]:
            return "**Agent is already stopped.** Use /start or click START AGENT to begin."
        return "**Stopping...** Use the /stop command or click STOP AGENT. It will finish the current step gracefully."

    if "lead" in lower and ("find" in lower or "get" in lower or "new" in lower or "scrape" in lower):
        return (f"**Lead Generation**\n\n"
                f"Current database: **{db_nums['total']:,} leads**\n\n"
                f"Sources available:\n"
                f"• Google Maps (Apify) — primary source\n"
                f"• Apollo.io — best email quality\n"
                f"• Google Search — directory scraping\n\n"
                f"Daily target: 1,000 leads. Use /run scrape to run the scraper or /start to begin the full pipeline.")

    if "error" in lower:
        errors = agent_state["errors"][-5:] if agent_state["errors"] else []
        if errors:
            error_text = "\n".join(f"• [{e.get('time','?')}] {e.get('msg','Unknown error')}" for e in errors)
            return f"**Recent Errors ({len(errors)}):**\n\n{error_text}"
        return "**No errors recorded** in this session."

    return (f"**WholesaleHunter Command Center**\n\n"
            f"I can help with:\n"
            f"• **Status** — Full pipeline and project report\n"
            f"• **Leads** — Find, analyze, and score leads\n"
            f"• **Reply rates** — Why they're low and how to fix\n"
            f"• **Segments** — Which countries/types perform best\n"
            f"• **Domains** — Email domain health and rotation\n"
            f"• **Actions** — Start/stop agent, run pipeline steps\n\n"
            f"Type /help to see all slash commands or ask anything in plain English.\n\n"
            f"DB: {db_nums['total']:,} leads | {db_nums['emailed']:,} emailed | {db_nums['reply_rate']}% reply rate")


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
                if params.get("state"):
                    filters["state"] = f"eq.{params['state'][0]}"
                if params.get("city"):
                    filters["city"] = f"eq.{params['city'][0]}"
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

        elif path == "/api/chat-analytics":
            # Structured analytics for the chat command center
            from modules.database import db
            analytics = {"funnel": {}, "top_countries": [], "domain_health": [], "recommendations": []}
            if db:
                try:
                    all_leads = db.select("leads", columns="country,lead_type,email_sent,replied,closed,score,source", limit=10000)
                    total = len(all_leads)
                    emailed = sum(1 for l in all_leads if l.get("email_sent"))
                    replied = sum(1 for l in all_leads if l.get("replied"))
                    closed = sum(1 for l in all_leads if l.get("closed"))
                    analytics["funnel"] = {
                        "total": total, "emailed": emailed, "replied": replied, "closed": closed,
                        "reply_rate": round(replied / emailed * 100, 1) if emailed else 0,
                        "close_rate": round(closed / emailed * 100, 1) if emailed else 0,
                    }
                    # Country breakdown
                    cs = {}
                    for l in all_leads:
                        c = l.get("country", "?")
                        if c not in cs: cs[c] = {"total":0,"emailed":0,"replied":0,"closed":0}
                        cs[c]["total"] += 1
                        if l.get("email_sent"): cs[c]["emailed"] += 1
                        if l.get("replied"): cs[c]["replied"] += 1
                        if l.get("closed"): cs[c]["closed"] += 1
                    analytics["top_countries"] = sorted(
                        [{"country":c, **s, "reply_rate": round(s["replied"]/s["emailed"]*100,1) if s["emailed"] else 0}
                         for c,s in cs.items()],
                        key=lambda x: -x["total"]
                    )[:10]
                    # Recommendations
                    recs = []
                    for c, s in cs.items():
                        if s["emailed"] >= 200 and s["closed"] == 0:
                            recs.append(f"Pause {c} — {s['emailed']} emails, 0 closes")
                    if analytics["funnel"]["reply_rate"] < 2:
                        recs.append("Reply rate below 2% — check domain health and email content")
                    analytics["recommendations"] = recs
                except Exception as e:
                    analytics["error"] = str(e)
            self._json_response(analytics)

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
