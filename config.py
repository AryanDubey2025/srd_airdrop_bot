import os
from dotenv import load_dotenv

# Load variables from .env (local) or Railway environment
load_dotenv()

# Telegram bot token
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Required channels (comma-separated in Railway Variables)
REQUIRED_CHANNELS = [
    c.strip().lstrip("@") for c in os.getenv("REQUIRED_CHANNELS", "").split(",") if c.strip()
]

# Blockchain / Web3 config
BSC_RPC = os.getenv("BSC_RPC")
ADMIN_PRIVATE_KEY = os.getenv("ADMIN_PRIVATE_KEY")
BEAM_CONTRACT = os.getenv("BEAM_CONTRACT")

# Rewards & withdrawal settings
WELCOME_REWARD_BEAM = int(os.getenv("WELCOME_REWARD_BEAM", "1"))   # reward on join
REFERRAL_REWARD_BEAM = int(os.getenv("REFERRAL_REWARD_BEAM", "1")) # reward per referral
REFERRALS_PER_WITHDRAWAL = int(os.getenv("REFERRALS_PER_WITHDRAWAL", "3")) # auto-withdraw threshold

