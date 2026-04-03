"""
WholesaleHunter v2 — Lead Qualification & Scoring Module
Uses Claude AI to score leads and tag them with metadata.
"""

import json
import logging
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from config import (
    ROZPER, SCORE_THRESHOLDS, TARGET_COUNTRIES,
)
from modules.database import update_lead, is_segment_paused
from modules.ai_client import ai_generate, is_ai_available

logger = logging.getLogger("wholesalehunter.qualifier")

# ═══════════════════════════════════════════════════════════════
# AI SCORING
# ═══════════════════════════════════════════════════════════════

SCORING_PROMPT = """You are a B2B lead scoring expert for Rozper, a wholesale VoIP carrier.

Rozper sells:
- CLI Routes (premium voice termination)
- CC Routes (call center routes)
- Origination (inbound DIDs)
- A2P SMS (bulk messaging)

Coverage: 190+ countries. Target buyers: VoIP providers, UCaaS/CCaaS providers, MNOs, MVNOs, call centers, wholesale resellers, ITSPs.

Score this lead from 0-100 based on how likely they are to buy Rozper's services:

Company: {company_name}
Domain: {company_domain}
Contact: {contact_name} ({contact_title})
Country: {country}
City: {city}
Type: {lead_type}
Description: {description}

Consider:
1. Does their business need voice routes? (VoIP provider = high, restaurant = 0)
2. Are they in a country where Rozper has strong coverage?
3. Is the contact person a decision-maker?
4. Company size/relevance to wholesale telecom

Return ONLY valid JSON:
{{
    "score": <0-100>,
    "reason": "<one sentence why>",
    "company_size": "<small|medium|enterprise>",
    "likely_products": ["<which Rozper products they'd need>"],
    "priority": "<high|medium|low>"
}}"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def score_lead_with_ai(lead: dict) -> dict:
    """Score a single lead using AI (Gemini or Anthropic)."""
    if not is_ai_available():
        return score_lead_rules(lead)

    prompt = SCORING_PROMPT.format(
        company_name=lead.get("company_name", ""),
        company_domain=lead.get("company_domain", ""),
        contact_name=lead.get("contact_name", ""),
        contact_title=lead.get("contact_title", ""),
        country=lead.get("country", ""),
        city=lead.get("city", ""),
        lead_type=lead.get("lead_type", ""),
        description=lead.get("keyword_used", ""),
    )

    try:
        text = ai_generate(prompt, max_tokens=300)
        if not text:
            return score_lead_rules(lead)

        # Parse JSON from response
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            result = json.loads(match.group())
        else:
            result = json.loads(text)

        return {
            "score": max(0, min(100, int(result.get("score", 0)))),
            "score_reason": result.get("reason", ""),
            "company_size": result.get("company_size", "small"),
        }
    except json.JSONDecodeError:
        logger.warning(f"AI returned non-JSON for {lead.get('company_domain', '?')}, falling back to rules")
        return score_lead_rules(lead)
    except Exception as e:
        logger.error(f"AI scoring error: {e}")
        return score_lead_rules(lead)


def score_lead_rules(lead: dict) -> dict:
    """Rule-based fallback scoring when AI is unavailable."""
    score = 30  # base score
    reasons = []

    # Lead type scoring
    type_scores = {
        "voip_provider": 30, "ucaas": 25, "ccaas": 25, "itsp": 25,
        "call_center": 20, "reseller": 20, "mno": 15, "mvno": 10, "other": 0,
    }
    type_bonus = type_scores.get(lead.get("lead_type", "other"), 0)
    score += type_bonus
    if type_bonus > 0:
        reasons.append(f"Relevant lead type: {lead['lead_type']}")

    # Has email = better
    if lead.get("contact_email"):
        score += 10
        reasons.append("Has contact email")

    # Has decision-maker title
    title = (lead.get("contact_title") or "").lower()
    if any(t in title for t in ["ceo", "cto", "vp", "director", "head", "manager"]):
        score += 10
        reasons.append("Decision-maker contact")

    # Country priority (top-converting countries get bonus)
    high_priority = ["UAE", "UK", "US", "Germany", "Netherlands", "Singapore"]
    if lead.get("country") in high_priority:
        score += 10
        reasons.append(f"High-priority country: {lead['country']}")

    # Has website (needed for form filling)
    if lead.get("website_url"):
        score += 5

    score = max(0, min(100, score))
    return {
        "score": score,
        "score_reason": "; ".join(reasons) if reasons else "Base score",
        "company_size": "small",
    }


# ═══════════════════════════════════════════════════════════════
# BATCH QUALIFICATION
# ═══════════════════════════════════════════════════════════════

def qualify_leads(leads: list[dict], use_ai: bool = True) -> list[dict]:
    """
    Score and qualify a batch of leads.
    Returns only leads that pass the minimum score threshold.
    Skips leads from paused segments.
    """
    qualified = []
    skipped_segment = 0
    skipped_score = 0

    for lead in leads:
        # Check if segment is paused
        if is_segment_paused("country", lead.get("country", "")):
            skipped_segment += 1
            continue
        if is_segment_paused("lead_type", lead.get("lead_type", "")):
            skipped_segment += 1
            continue

        # Score the lead
        if use_ai and is_ai_available():
            scoring = score_lead_with_ai(lead)
        else:
            scoring = score_lead_rules(lead)

        lead["score"] = scoring["score"]
        lead["score_reason"] = scoring["score_reason"]
        lead["company_size"] = scoring.get("company_size", "small")

        # Apply threshold
        if lead["score"] < SCORE_THRESHOLDS["skip_below"]:
            skipped_score += 1
            continue

        if lead["score"] >= SCORE_THRESHOLDS["min_qualify"]:
            qualified.append(lead)
        else:
            skipped_score += 1

    logger.info(
        f"Qualification: {len(qualified)} qualified, "
        f"{skipped_score} low-score, {skipped_segment} paused-segment"
    )
    return qualified


def re_score_lead(lead_id: str, lead: dict) -> None:
    """Re-score an existing lead (used during weekly re-evaluation)."""
    scoring = score_lead_with_ai(lead) if is_ai_available() else score_lead_rules(lead)
    update_lead(lead_id, {
        "score": scoring["score"],
        "score_reason": scoring["score_reason"],
        "company_size": scoring.get("company_size", "small"),
    })
