import os
from dotenv import load_dotenv

# Load .env file if present (Railway also uses environment vars)
load_dotenv()

# ===============================================================
# Telegram Bot
# ===============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")   # must be set in .env or Railway Variables

# Required channels: you can use @username or numeric chat IDs (-100…)
# Example with usernames:
# REQUIRED_CHANNELS = ["srdexchange", "srdexchangeglobal", "srdearning"]
#
# Example with chat IDs (recommended if usernames fail):
# REQUIRED_CHANNELS = ["-1001780887211", "-1001674515489", "-1001482940867"]

REQUIRED_CHANNELS = [
    c.strip().lstrip("@")
    for c in os.getenv(
        "REQUIRED_CHANNELS",
        "srdexchange,srdexchangeglobal,srdearning"
    ).split(",")
    if c.strip()
]

# ===============================================================
# Blockchain (BSC / BEP20)
# ===============================================================

BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")  # default BSC RPC
ADMIN_PRIVATE_KEY = os.getenv("ADMIN_PRIVATE_KEY", "")
BEAM_CONTRACT = os.getenv("BEAM_CONTRACT", "")

# ===============================================================
# Rewards Config
# ===============================================================

WELCOME_REWARD_BEAM = int(os.getenv("WELCOME_REWARD_BEAM", "5"))      # reward for joining
REFERRAL_REWARD_BEAM = int(os.getenv("REFERRAL_REWARD_BEAM", "5"))    # reward per referral
REFERRALS_PER_WITHDRAWAL = int(os.getenv("REFERRALS_PER_WITHDRAWAL", "3"))  # min refs for auto withdraw
