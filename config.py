import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

REQUIRED_CHANNELS = [
    c.strip() for c in os.getenv("REQUIRED_CHANNELS", "").split(",") if c.strip()
]

BSC_RPC = os.getenv("BSC_RPC")
ADMIN_PRIVATE_KEY = os.getenv("ADMIN_PRIVATE_KEY")
BEAM_CONTRACT = os.getenv("BEAM_CONTRACT")

WELCOME_REWARD_BEAM = int(os.getenv("WELCOME_REWARD_BEAM", "5"))
REFERRAL_REWARD_BEAM = int(os.getenv("REFERRAL_REWARD_BEAM", "5"))
REFERRALS_PER_WITHDRAWAL = int(os.getenv("REFERRALS_PER_WITHDRAWAL", "3"))
