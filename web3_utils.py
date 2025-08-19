from web3 import Web3
from eth_account import Account
from config import BSC_RPC, ADMIN_PRIVATE_KEY, BEAM_CONTRACT

# Minimal ERC-20 ABI
ERC20_ABI = [
    {"name": "decimals", "outputs": [{"type": "uint8", "name": ""}],
     "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "balanceOf", "outputs": [{"type": "uint256", "name": ""}],
     "inputs": [{"type": "address", "name": "account"}],
     "stateMutability": "view", "type": "function"},
    {"name": "transfer", "outputs": [{"type": "bool", "name": ""}],
     "inputs": [{"type": "address", "name": "to"}, {"type": "uint256", "name": "amount"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# Connect to BSC
w3 = Web3(Web3.HTTPProvider(BSC_RPC, request_kwargs={"timeout": 30}))
if not w3.is_connected():
    raise RuntimeError("Web3 not connected. Check BSC_RPC or internet connection.")

# Admin account
admin_account = Account.from_key(ADMIN_PRIVATE_KEY)
admin_address = admin_account.address

# Token contract
contract = w3.eth.contract(address=Web3.to_checksum_address(BEAM_CONTRACT), abi=ERC20_ABI)

# Cache decimals
_token_decimals = None
def token_decimals():
    global _token_decimals
    if _token_decimals is None:
        _token_decimals = contract.functions.decimals().call()
    return _token_decimals

def to_wei_tokens(amount_tokens: int):
    return int(amount_tokens) * (10 ** token_decimals())

def send_tokens(to_addr: str, amount_tokens: int) -> str:
    to = Web3.to_checksum_address(to_addr)
    nonce = w3.eth.get_transaction_count(admin_address)
    tx = contract.functions.transfer(to, to_wei_tokens(amount_tokens)).build_transaction({
        "from": admin_address,
        "nonce": nonce,
        "gas": 100000,
        "maxFeePerGas": w3.to_wei("3", "gwei"),
        "maxPriorityFeePerGas": w3.to_wei("1", "gwei"),
        "chainId": 56  # BSC mainnet
    })
    signed = admin_account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise RuntimeError("Token transfer failed")
    return tx_hash.hex()

def is_address(addr: str) -> bool:
    try:
        return Web3.is_address(addr)
    except Exception:
        return False

def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)
