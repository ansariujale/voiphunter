"""
WholesaleHunter v2 — Configuration
All API keys, thresholds, and settings in one place.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# API KEYS & CREDENTIALS
# ═══════════════════════════════════════════════════════════════

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # service_role key

APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_WORKSPACE_ID = os.getenv("INSTANTLY_WORKSPACE_ID", "")

# SMTP (Gmail) — fallback when Instantly is not configured
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Sajid Kapadia")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ═══════════════════════════════════════════════════════════════
# ROZPER BUSINESS INFO (used in emails & form fills)
# ═══════════════════════════════════════════════════════════════

ROZPER = {
    "company_name": "Rozper",
    "contact_name": "Sajid Kapadia",
    "contact_email": "sajid@rozper.com",
    "website": "https://rozper.com",
    "products": ["CLI Routes", "CC Routes", "Origination (DIDs)", "A2P SMS"],
    "coverage": "190+ countries",
    "usp": "Premium CLI voice routes — free test minutes, competitive pricing, wide coverage",
    "hook": "Free test minutes — prove quality before any commitment",
}

# ═══════════════════════════════════════════════════════════════
# SCRAPING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_LEAD_TARGET = 1000

# Target buyer types
LEAD_TYPES = [
    "voip_provider",
    "ucaas",
    "ccaas",
    "mno",
    "mvno",
    "call_center",
    "reseller",
    "itsp",
]

# Target countries (priority order — high-converting first)
TARGET_COUNTRIES = [
    "UAE", "UK", "US", "India", "Germany", "France", "Netherlands",
    "South Africa", "Nigeria", "Kenya", "Saudi Arabia", "Singapore",
    "Malaysia", "Philippines", "Bangladesh", "Pakistan", "Turkey",
    "Egypt", "Ghana", "Tanzania", "Brazil", "Mexico", "Colombia",
]

# Search keywords templates (combined with country)
SEARCH_KEYWORDS = [
    "wholesale voice {country}",
    "VoIP provider {country}",
    "voice termination {country}",
    "SIP trunking provider {country}",
    "call center {country}",
    "telecom carrier {country}",
    "wholesale routes {country}",
    "CLI routes {country}",
    "international voice {country}",
    "UCaaS provider {country}",
]

# Apollo.io job titles to search
APOLLO_JOB_TITLES = [
    "CEO", "CTO", "VP Sales", "VP Business Development",
    "Director of Carrier Relations", "Head of Wholesale",
    "Carrier Manager", "Voice Operations Manager",
    "Head of Interconnect", "Director of Telecom",
]

# Apollo.io industry keywords
APOLLO_INDUSTRIES = [
    "Telecommunications", "VoIP", "Internet Telephony",
    "Unified Communications", "Contact Center",
]

# ═══════════════════════════════════════════════════════════════
# EMAIL SETTINGS (Instantly.dev)
# ═══════════════════════════════════════════════════════════════

DAILY_EMAIL_TARGET = 1000
EMAILS_PER_DOMAIN = 65  # safe limit per sending domain

# Sending domains (outreach only — never rozper.com)
SENDING_DOMAINS = [
    "getrozper.com",
    "rozper.io",
    "rozpervoip.com",
    "rozpertel.com",
    "tryrozper.com",
    "rozpervoice.com",
    "rozperglobal.com",
    "hellorozper.com",
]

# Follow-up sequence timing (days after initial email)
FOLLOWUP_SCHEDULE = {
    1: "initial",      # Day 1: Intro + USP + free test
    3: "quality",      # Day 3: Quality angle — "High ASR CLI to [region]"
    7: "social_proof",  # Day 7: Social proof — "Carrying X million min"
    14: "breakup",     # Day 14: Breakup — "No pressure, offer open"
}

# ═══════════════════════════════════════════════════════════════
# FORM FILLING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_FORM_TARGET = 1000

FORM_PATHS_TO_TRY = [
    "/contact", "/contact-us", "/inquiry", "/get-quote",
    "/partnership", "/partners", "/get-in-touch",
    "/request-quote", "/reach-us", "/talk-to-sales",
    "/sales", "/connect", "/enquiry",
]

FORM_MESSAGE_TEMPLATE = (
    "Hi, I'm {contact_name} from {company_name}. "
    "We offer premium CLI voice routes to {coverage} with competitive pricing. "
    "Would you be interested in a free test to compare quality on your key destinations? "
    "Happy to share rate sheets — just let me know your top routes. "
    "Best, {contact_name}"
)

# ═══════════════════════════════════════════════════════════════
# LEAD SCORING THRESHOLDS
# ═══════════════════════════════════════════════════════════════

SCORE_THRESHOLDS = {
    "min_qualify": 40,        # minimum score to qualify a lead
    "high_priority": 70,      # high-priority leads
    "skip_below": 20,         # auto-skip leads below this score
}

# ═══════════════════════════════════════════════════════════════
# AUTO-EXCLUSION RULES (Intelligence System)
# ═══════════════════════════════════════════════════════════════

AUTO_EXCLUSION = {
    "country_pause_after_leads": 200,       # pause country if 0 closes after N leads
    "lead_type_min_close_rate": 0.005,      # 0.5% — deprioritize below this
    "lead_type_min_sample": 500,            # need N leads before judging
    "source_min_reply_rate": 0.01,          # 1% — reduce volume below this
    "source_min_sample": 1000,              # need N sends before judging
    "email_domain_min_open_rate": 0.10,     # rotate domain if open rate < 10%
}

# ═══════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════

WEEKLY_REPORT_DAY = "sunday"  # day of week to generate intelligence report
NOTIFICATION_EMAIL = "aujale30@gmail.com"  # where to send hot lead alerts

# Current sending email (for testing — will be replaced with mailbox rotation later)
SENDER_EMAIL = "aujale30@gmail.com"

# ═══════════════════════════════════════════════════════════════
# RATE LIMITS & SAFETY
# ═══════════════════════════════════════════════════════════════

SCRAPE_DELAY_SECONDS = 2        # delay between web scraping requests
FORM_FILL_DELAY_SECONDS = 5     # delay between form submissions
MAX_RETRIES = 3                 # max retries per operation
REQUEST_TIMEOUT = 30            # HTTP request timeout in seconds

# ═══════════════════════════════════════════════════════════════
# EMAIL VARIANT GENERATION & SCORING
# ═══════════════════════════════════════════════════════════════

VARIANTS_PER_LEAD = 5           # number of AI-generated email variants per lead

# Warmup schedule: day number → max emails per domain
WARMUP_SCHEDULE = {
    1: 5, 2: 8, 3: 12, 4: 18, 5: 25,
    6: 30, 7: 35, 8: 40, 9: 45, 10: 50,
    11: 52, 12: 54, 13: 56, 14: 58,
    15: 60, 16: 61, 17: 62, 18: 63, 19: 64, 20: 64, 21: 65,
}  # after day 21: EMAILS_PER_DOMAIN (65)

# Email scoring weights (each dimension 0-25, total 0-100)
EMAIL_SCORING_WEIGHTS = {
    "subject": 25,
    "personalization": 25,
    "cta": 25,
    "spam_safety": 25,
}

# Spam trigger words — presence in subject/body reduces spam_safety score
SPAM_TRIGGERS = {
    "act now", "limited time", "click here", "buy now", "order now",
    "urgent", "congratulations", "winner", "guarantee", "no obligation",
    "risk-free", "special promotion", "exclusive deal", "100%", "amazing",
    "incredible offer", "lowest price", "earn money", "cash bonus",
    "double your", "apply now", "sign up free", "subscribe now",
    "no cost", "no fees", "once in a lifetime", "don't miss",
    "for free", "zero risk",
}

# Junk email domains (personal emails, not company — skip these leads)
JUNK_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "yandex.com",
    "live.com", "msn.com", "inbox.com", "gmx.com",
}

# Email send delay range (seconds) — random between min and max to mimic human
EMAIL_SEND_DELAY = (2, 8)
