"""
Automated Wallet Manager v13.0
- Claims creator fees from pump.fun tokens
- Swaps SOL to reward token (NASDAQ) via Jupiter
- Distributes rewards to token holders based on tier ranking
- Token-2022 compatible (auto-detects token program)
- Discord webhook notifications
"""

import os
import time
import requests
import base64
from datetime import datetime, timezone
from typing import List, Dict, Optional, Set
from dotenv import load_dotenv
from colorama import init, Fore, Style
import re
import struct

# Solana imports
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYS_PROGRAM_ID
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.message import Message
from solders.transaction import Transaction as LegacyTx

# Initialize
init(autoreset=True)
load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    # Timing
    CHECK_INTERVAL_MINUTES = int(os.getenv('CHECK_INTERVAL_MINUTES', '5'))
    
    # Wallet
    PRIVATE_KEY = os.getenv('PRIVATE_KEY')
    PUBLIC_KEY = os.getenv('PUBLIC_KEY')
    
    # RPC & APIs
    RPC_ENDPOINT = os.getenv('RPC_ENDPOINT')
    HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')
    
    # Token Mints
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    NASDAQ_MINT = os.getenv('NASDAQ_MINT', '')
    TRACKED_TOKEN_MINT = os.getenv('TRACKED_TOKEN_MINT', '')
    
    # Token decimals (fetched dynamically at runtime)
    NASDAQ_DECIMALS = None
    TRACKED_TOKEN_DECIMALS = 6
    
    # Program IDs
    TOKEN_PROGRAM_ID = Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
    TOKEN_2022_PROGRAM_ID = Pubkey.from_string('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
    ACTUAL_TOKEN_PROGRAM = None
    
    # Swap Settings
    SWAP_PERCENTAGE = 90
    MIN_SOL_RESERVE = float(os.getenv('MIN_SOL_RESERVE', '0.05'))
    SLIPPAGE_BPS = 50
    
    # Manual Blacklist (add wallet addresses to exclude from distributions)
    MANUAL_BLACKLIST = []
    
    # Distribution Tiers (based on HOLDER RANK, not % of supply)
    DISTRIBUTION_TIERS = {
        (0, 1): 25,      # Top 1% of holders get 25% of rewards
        (1, 2): 15,      # Next 1% get 15%
        (2, 5): 12,      # Next 3% get 12%
        (5, 10): 10,     # Next 5% get 10%
        (10, 20): 12,    # Next 10% get 12%
        (20, 30): 8,     # Next 10% get 8%
        (30, 50): 5,     # Next 20% get 5%
        (50, 70): 5,     # Next 20% get 5%
        (70, 100): 8     # Bottom 30% get 8%
    }
    
    # Settings
    REQUIRE_CONFIRMATION = False
    
    # Discord
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')
    
    # APIs
    JUPITER_QUOTE_API = 'https://lite-api.jup.ag/swap/v1/quote'
    JUPITER_SWAP_API = 'https://lite-api.jup.ag/swap/v1/swap'
    PUMP_FUN_API = 'https://frontend-api-v3.pump.fun/coins/'
    
    LAMPORTS_PER_SOL = 1_000_000_000
    NASDAQ_PRICE_USD = 658.0


# ============================================================================
# UTILITIES
# ============================================================================

def format_token_amount(raw_amount: int, decimals: int) -> str:
    actual_amount = raw_amount / (10 ** decimals)
    
    if actual_amount >= 1_000_000_000:
        return f"{actual_amount / 1_000_000_000:.2f}B"
    elif actual_amount >= 1_000_000:
        return f"{actual_amount / 1_000_000:.2f}M"
    elif actual_amount >= 1_000:
        return f"{actual_amount / 1_000:.2f}K"
    else:
        return f"{actual_amount:.2f}"


def detect_token_program(rpc_client: Client, mint: Pubkey):
    """Detect whether mint is Token Program or Token-2022 and cache result."""
    try:
        account_info = rpc_client.get_account_info(mint)
        if account_info.value:
            owner = account_info.value.owner
            if str(owner) == str(Config.TOKEN_2022_PROGRAM_ID):
                Config.ACTUAL_TOKEN_PROGRAM = Config.TOKEN_2022_PROGRAM_ID
                print(f"{Fore.CYAN}Token program: Token-2022{Style.RESET_ALL}")
            else:
                Config.ACTUAL_TOKEN_PROGRAM = Config.TOKEN_PROGRAM_ID
                print(f"{Fore.CYAN}Token program: SPL Token{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.YELLOW}Could not detect token program, defaulting to SPL Token: {e}{Style.RESET_ALL}")
        Config.ACTUAL_TOKEN_PROGRAM = Config.TOKEN_PROGRAM_ID


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    token_prog = Config.ACTUAL_TOKEN_PROGRAM or Config.TOKEN_PROGRAM_ID
    seeds = [
        bytes(owner),
        bytes(token_prog),
        bytes(mint)
    ]
    
    return Pubkey.find_program_address(
        seeds,
        Config.ASSOCIATED_TOKEN_PROGRAM_ID
    )[0]


def fetch_token_decimals(rpc_client: Client, mint: Pubkey) -> int:
    try:
        account_info = rpc_client.get_account_info(mint)
        if account_info.value:
            data = account_info.value.data
            decimals = data[44]
            return decimals
        return 6
    except Exception as e:
        print(f"{Fore.YELLOW}Could not fetch decimals, using 6: {e}{Style.RESET_ALL}")
        return 6


# ============================================================================
# BLACKLIST MANAGER
# ============================================================================

class BlacklistManager:
    def __init__(self, tracked_mint: str):
        self.tracked_mint = tracked_mint
        self.blacklist: Set[str] = set(Config.MANUAL_BLACKLIST)
        if self.blacklist:
            print(f"{Fore.CYAN}Manual blacklist: {len(self.blacklist)} addresses{Style.RESET_ALL}")
    
    def fetch_blacklist(self) -> Set[str]:
        try:
            url = f"{Config.PUMP_FUN_API}{self.tracked_mint}"
            response = requests.get(url, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                api_addresses = self._extract_addresses(data)
                self.blacklist = self.blacklist.union(api_addresses)
                print(f"{Fore.GREEN}✅ Blacklist: {len(self.blacklist)} total (API: {len(api_addresses)}, Manual: {len(Config.MANUAL_BLACKLIST)}){Style.RESET_ALL}")
            
            return self.blacklist
        except:
            return self.blacklist
    
    def _extract_addresses(self, data: dict) -> Set[str]:
        addresses = set()
        pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
        found = re.findall(pattern, str(data))
        
        for addr in found:
            if 32 <= len(addr) <= 44:
                addresses.add(addr)
        
        return addresses
    
    def is_blacklisted(self, address: str) -> bool:
        return address in self.blacklist


# ============================================================================
# DISCORD LOGGER
# ============================================================================

class DiscordLogger:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.enabled = bool(webhook_url)
    
    def send(self, title: str, description: str, color: int = 0x3498db, fields: List[Dict] = None):
        if not self.enabled:
            return
        
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fields": fields or []
        }
        
        try:
            requests.post(self.webhook_url, json={"embeds": [embed]}, timeout=10)
            print(f"{Fore.GREEN}  ✓ Discord sent{Style.RESET_ALL}")
        except:
            pass
    
    def send_long_message(self, title: str, parts: List[str], color: int = 0x3498db):
        if not self.enabled:
            return
        
        embeds = [{"title": title, "description": parts[0], "color": color}]
        for part in parts[1:]:
            embeds.append({"description": part, "color": color})
        
        try:
            requests.post(self.webhook_url, json={"embeds": embeds[:10]}, timeout=10)
        except:
            pass
    
    def log_balance_summary(self, claimed: float, wallet: float, combined: float, swap_amount: float, nasdaq_received: float):
        self.send("Balance & Swap Summary", "Transaction completed", 0x3498db, [
            {"name": "Claimed SOL", "value": f"`{claimed:.6f}`", "inline": True},
            {"name": "Wallet SOL", "value": f"`{wallet:.6f}`", "inline": True},
            {"name": "Combined SOL", "value": f"`{combined:.6f}`", "inline": True},
            {"name": "Swapped (90%)", "value": f"`{swap_amount:.6f} SOL`", "inline": True},
            {"name": "NASDAQ Received", "value": f"`{nasdaq_received:.9f} NASDAQ`", "inline": True}
        ])
    
    def log_distribution_summary(self, distributions: List[Dict], total_nasdaq: float):
        if not distributions:
            return
        
        parts = []
        
        header = f"**DISTRIBUTION SUMMARY**\n\n"
        header += f"**Total Recipients:** `{len(distributions)}`\n"
        header += f"**Total NASDAQ:** `{total_nasdaq:.9f}`\n\n"
        parts.append(header)
        
        sorted_dist = sorted(distributions, key=lambda x: x['rank'])
        
        current_chunk = "**Distribution Details:**\n\n"
        for d in sorted_dist:
            balance_formatted = format_token_amount(d['balance'], Config.TRACKED_TOKEN_DECIMALS)
            nasdaq_amount = d['amount']
            
            line = f"`#{d['rank']}` **{d['address']}**\n"
            line += f"   Holds: `{balance_formatted}` ({d['pct_of_supply']:.2f}%) | Gets: `{nasdaq_amount:.9f} NASDAQ` | Tier: `{d['tier']}`\n\n"
            
            if len(current_chunk) + len(line) > 1800:
                parts.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        
        if current_chunk:
            parts.append(current_chunk)
        
        self.send_long_message("Distribution Plan", parts, 0xe67e22)
    
    def log_transfers_complete(self, count: int, signatures: List[str]):
        sig_links = "\n".join([f"[TX {i+1}](https://solscan.io/tx/{sig})" for i, sig in enumerate(signatures[:10])])
        self.send("Transfers Complete", f"Successfully sent NASDAQ to {count} addresses", 0x2ecc71, [
            {"name": "Transactions", "value": sig_links or "None", "inline": False}
        ])


# ============================================================================
# FEE CLAIMER
# ============================================================================

class FeeClaimer:
    def __init__(self, keypair: Keypair, rpc_client: Client):
        self.keypair = keypair
        self.rpc_client = rpc_client
        self.public_key = str(keypair.pubkey())
    
    def get_balance(self) -> float:
        try:
            balance = self.rpc_client.get_balance(self.keypair.pubkey()).value
            return balance / Config.LAMPORTS_PER_SOL
        except:
            return 0.0
    
    def claim_fees(self) -> Optional[Dict]:
        try:
            print(f"\n{Fore.CYAN}→ Claiming fees...{Style.RESET_ALL}")
            
            balance_before = self.get_balance()
            
            response = requests.post(
                "https://pumpportal.fun/api/trade-local",
                json={
                    "publicKey": self.public_key,
                    "action": "collectCreatorFee",
                    "priorityFee": 0.0001,
                    "pool": "pump"
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None
            
            tx_data = response.content
            recent_blockhash = self.rpc_client.get_latest_blockhash(Confirmed).value.blockhash
            
            tx = VersionedTransaction.from_bytes(tx_data)
            from solders.message import MessageV0
            new_message = MessageV0(
                header=tx.message.header,
                account_keys=tx.message.account_keys,
                recent_blockhash=recent_blockhash,
                instructions=tx.message.instructions,
                address_table_lookups=tx.message.address_table_lookups
            )
            
            new_tx = VersionedTransaction(new_message, [self.keypair])
            opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3)
            result = self.rpc_client.send_raw_transaction(bytes(new_tx), opts=opts)
            
            signature_str = str(result.value)
            sig_obj = Signature.from_string(signature_str)
            self.rpc_client.confirm_transaction(sig_obj, commitment=Confirmed)
            
            time.sleep(2)
            balance_after = self.get_balance()
            claimed_amount = max(balance_after - balance_before, 0.001)
            
            print(f"{Fore.GREEN}✅ Claimed: {claimed_amount:.6f} SOL{Style.RESET_ALL}")
            
            return {'amount': claimed_amount, 'signature': signature_str}
            
        except Exception as e:
            print(f"{Fore.RED}✗ Claim error: {e}{Style.RESET_ALL}")
            return None


# ============================================================================
# TOKEN SWAPPER (SOL -> NASDAQ via Jupiter)
# ============================================================================

class TokenSwapper:
    def __init__(self, keypair: Keypair, rpc_client: Client):
        self.keypair = keypair
        self.rpc_client = rpc_client
        self.public_key = str(keypair.pubkey())
    
    def get_balance(self) -> float:
        try:
            balance = self.rpc_client.get_balance(self.keypair.pubkey()).value
            return balance / Config.LAMPORTS_PER_SOL
        except:
            return 0.0
    
    def swap_to_nasdaq(self, claimed_amount: float, wallet_balance: float) -> Optional[Dict]:
        combined_sol = claimed_amount + wallet_balance
        
        print(f"\n{Fore.CYAN}Balance Summary:{Style.RESET_ALL}")
        print(f"  Claimed SOL: {claimed_amount:.6f}")
        print(f"  Wallet SOL: {wallet_balance:.6f}")
        print(f"  Combined SOL: {combined_sol:.6f}")
        
        available = combined_sol - Config.MIN_SOL_RESERVE
        
        if available <= 0:
            print(f"{Fore.RED}  ✗ Insufficient SOL (need {Config.MIN_SOL_RESERVE} reserve){Style.RESET_ALL}")
            return None
        
        swap_amount = (available * Config.SWAP_PERCENTAGE) / 100
        
        print(f"{Fore.GREEN}  ✓ Swapping {Config.SWAP_PERCENTAGE}%: {swap_amount:.6f} SOL{Style.RESET_ALL}")
        print(f"{Fore.CYAN}  Keeping: {combined_sol - swap_amount:.6f} SOL{Style.RESET_ALL}")
        
        try:
            lamports = int(swap_amount * Config.LAMPORTS_PER_SOL)
            url = f"{Config.JUPITER_QUOTE_API}?inputMint={Config.SOL_MINT}&outputMint={Config.NASDAQ_MINT}&amount={lamports}&slippageBps={Config.SLIPPAGE_BPS}"
            response = requests.get(url, timeout=15)
            
            if response.status_code != 200:
                print(f"{Fore.RED}✗ Quote failed{Style.RESET_ALL}")
                return None
            
            quote = response.json()
            out_lamports = int(quote['outAmount'])
            
            if Config.NASDAQ_DECIMALS is None:
                Config.NASDAQ_DECIMALS = fetch_token_decimals(self.rpc_client, Pubkey.from_string(Config.NASDAQ_MINT))
                print(f"{Fore.CYAN}  NASDAQ token decimals: {Config.NASDAQ_DECIMALS}{Style.RESET_ALL}")
            
            out_amount = out_lamports / (10 ** Config.NASDAQ_DECIMALS)
            
            print(f"{Fore.GREEN}  ✓ Quote: {out_amount:.9f} NASDAQ ({out_lamports} lamports){Style.RESET_ALL}")
            
            swap_response = requests.post(
                Config.JUPITER_SWAP_API,
                json={
                    'quoteResponse': quote,
                    'userPublicKey': self.public_key,
                    'wrapAndUnwrapSol': True,
                    'dynamicComputeUnitLimit': True,
                    'prioritizationFeeLamports': {'priorityLevelWithMaxLamports': {'maxLamports': 10000000, 'priorityLevel': 'veryHigh'}}
                },
                timeout=30
            )
            
            if swap_response.status_code != 200:
                print(f"{Fore.RED}✗ Swap failed{Style.RESET_ALL}")
                return None
            
            swap_data = swap_response.json()
            tx_bytes = base64.b64decode(swap_data['swapTransaction'])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            
            opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3)
            result = self.rpc_client.send_raw_transaction(bytes(signed_tx), opts=opts)
            
            signature_str = str(result.value)
            sig_obj = Signature.from_string(signature_str)
            self.rpc_client.confirm_transaction(sig_obj, commitment=Confirmed)
            
            print(f"{Fore.GREEN}✅ Swapped {swap_amount:.6f} SOL → {out_amount:.9f} NASDAQ{Style.RESET_ALL}")
            
            return {
                'signature': signature_str,
                'output_amount': out_amount,
                'swap_amount': swap_amount,
                'claimed': claimed_amount,
                'wallet': wallet_balance,
                'combined': combined_sol
            }
            
        except Exception as e:
            print(f"{Fore.RED}✗ Swap error: {e}{Style.RESET_ALL}")
            return None


# ============================================================================
# HOLDER ANALYZER
# ============================================================================

class HolderAnalyzer:
    def __init__(self, helius_api_key: str):
        self.url = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
    
    def get_all_holders(self, mint_address: str, blacklist_manager: BlacklistManager) -> List[Dict]:
        all_holders = {}
        cursor = None
        
        print(f"\n{Fore.CYAN}→ Fetching holders...{Style.RESET_ALL}")
        
        while True:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "getTokenAccounts",
                    "params": {"mint": mint_address, "limit": 1000, "options": {"showZeroBalance": False}}
                }
                
                if cursor:
                    payload["params"]["cursor"] = cursor
                
                response = requests.post(self.url, json=payload, timeout=30)
                data = response.json()
                
                if "error" in data or "result" not in data:
                    break
                
                result = data["result"]
                token_accounts = result.get("token_accounts", [])
                
                for account in token_accounts:
                    owner = account["owner"]
                    amount = int(account["amount"])
                    all_holders[owner] = all_holders.get(owner, 0) + amount
                
                cursor = result.get("cursor")
                if not cursor:
                    break
                    
            except:
                break
        
        holders_list = []
        blacklisted_count = 0
        
        for addr, balance in all_holders.items():
            is_blacklisted = blacklist_manager.is_blacklisted(addr)
            if is_blacklisted:
                blacklisted_count += 1
            
            holders_list.append({"address": addr, "balance": balance, "blacklisted": is_blacklisted})
        
        holders_list.sort(key=lambda x: x['balance'], reverse=True)
        
        print(f"{Fore.GREEN}✅ {len(holders_list)} holders ({blacklisted_count} blacklisted){Style.RESET_ALL}")
        
        return holders_list


# ============================================================================
# DISTRIBUTOR
# ============================================================================

class Distributor:
    def __init__(self, keypair: Keypair, rpc_client: Client):
        self.keypair = keypair
        self.rpc_client = rpc_client
        self.nasdaq_mint = Pubkey.from_string(Config.NASDAQ_MINT)
    
    def calculate_distribution(self, holders: List[Dict], total_nasdaq: float) -> List[Dict]:
        if not holders:
            print(f"{Fore.RED}  ✗ No holders{Style.RESET_ALL}")
            return []
        
        if total_nasdaq <= 0:
            print(f"{Fore.RED}  ✗ No NASDAQ to distribute ({total_nasdaq:.9f}){Style.RESET_ALL}")
            return []
        
        eligible = [h for h in holders if not h.get('blacklisted', False)]
        
        if not eligible:
            print(f"{Fore.RED}  ✗ No eligible holders{Style.RESET_ALL}")
            return []
        
        total_supply = 1_000_000_000 * (10 ** Config.TRACKED_TOKEN_DECIMALS)
        total_holders = len(eligible)
        
        print(f"\n{Fore.CYAN}Distribution Calculation:{Style.RESET_ALL}")
        print(f"  Eligible: {len(eligible)}/{len(holders)}")
        print(f"  NASDAQ: {total_nasdaq:.9f}")
        
        for holder in eligible:
            holder['pct_of_supply'] = (holder['balance'] / total_supply) * 100
        
        eligible.sort(key=lambda x: x['balance'], reverse=True)
        
        tier_holders = {}
        
        for rank, holder in enumerate(eligible, start=1):
            holder_percentile = (rank / total_holders) * 100
            
            tier_assigned = False
            for (start, end) in sorted(Config.DISTRIBUTION_TIERS.keys()):
                if start < holder_percentile <= end:
                    tier_name = f"Top {start}-{end}%"
                    if tier_name not in tier_holders:
                        tier_holders[tier_name] = []
                    tier_holders[tier_name].append((holder, rank))
                    tier_assigned = True
                    break
            
            if not tier_assigned:
                tier_name = "Top 70-100%"
                if tier_name not in tier_holders:
                    tier_holders[tier_name] = []
                tier_holders[tier_name].append((holder, rank))
        
        distributions = []
        
        for tier_name, tier_list in tier_holders.items():
            tier_key = None
            for (start, end), reward_pct in Config.DISTRIBUTION_TIERS.items():
                if f"Top {start}-{end}%" == tier_name:
                    tier_key = (start, end)
                    break
            
            if not tier_key:
                continue
            
            reward_pct = Config.DISTRIBUTION_TIERS[tier_key]
            tier_allocation = (total_nasdaq * reward_pct) / 100
            nasdaq_per_holder = tier_allocation / len(tier_list)
            
            for holder, rank in tier_list:
                distributions.append({
                    'address': holder['address'],
                    'amount': nasdaq_per_holder,
                    'tier': tier_name,
                    'balance': holder['balance'],
                    'pct_of_supply': holder['pct_of_supply'],
                    'rank': rank
                })
        
        total_distributed = sum(d['amount'] for d in distributions)
        
        print(f"{Fore.GREEN}  Recipients: {len(distributions)}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}  Total: {total_distributed:.9f} NASDAQ{Style.RESET_ALL}")
        
        return distributions
    
    def show_distribution_table(self, distributions: List[Dict]):
        print(f"\n{Fore.CYAN}{'='*110}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}DISTRIBUTION PLAN{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*110}{Style.RESET_ALL}\n")
        
        sorted_dist = sorted(distributions, key=lambda x: x['rank'])
        
        print(f"{Fore.YELLOW}{'Rank':<6} {'Address':<44} {'Holds':<12} {'% Supply':<10} {'Gets NASDAQ':<18} {'Tier':<15}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}{'-'*110}{Style.RESET_ALL}")
        
        for d in sorted_dist:
            balance_fmt = format_token_amount(d['balance'], Config.TRACKED_TOKEN_DECIMALS)
            print(f"#{d['rank']:<5} {d['address']:<44} {balance_fmt:<12} {d['pct_of_supply']:<10.2f} {d['amount']:<18.9f} {d['tier']:<15}")
        
        print(f"\n{Fore.GREEN}Total Recipients: {len(distributions)}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Total NASDAQ: {sum(d['amount'] for d in distributions):.9f}{Style.RESET_ALL}\n")

    def get_nasdaq_balance(self) -> float:
        """Fetch wallet's NASDAQ token balance via RPC getTokenAccountsByOwner."""
        try:
            owner = str(self.keypair.pubkey())
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    owner,
                    {"mint": Config.NASDAQ_MINT},
                    {"encoding": "jsonParsed"}
                ]
            }
            resp = requests.post(Config.RPC_ENDPOINT, json=payload, timeout=15)
            data = resp.json()
            accounts = data.get("result", {}).get("value", [])
            total = 0.0
            for acct in accounts:
                info = acct["account"]["data"]["parsed"]["info"]
                total += float(info["tokenAmount"]["uiAmount"] or 0)
            return total
        except Exception as e:
            print(f"{Fore.RED}  Could not fetch NASDAQ balance: {e}{Style.RESET_ALL}")
            return 0.0

    def send_tokens(self, distributions: List[Dict]) -> List[str]:
        print(f"\n{Fore.CYAN}━━━ SENDING TOKENS ━━━{Style.RESET_ALL}\n")
        
        if Config.NASDAQ_DECIMALS is None:
            Config.NASDAQ_DECIMALS = fetch_token_decimals(self.rpc_client, self.nasdaq_mint)
            print(f"{Fore.CYAN}NASDAQ token decimals: {Config.NASDAQ_DECIMALS}{Style.RESET_ALL}\n")
        
        token_prog = Config.ACTUAL_TOKEN_PROGRAM or Config.TOKEN_PROGRAM_ID
        source_ata = get_associated_token_address(self.keypair.pubkey(), self.nasdaq_mint)
        
        signatures = []
        
        for i, d in enumerate(distributions, 1):
            try:
                recipient = Pubkey.from_string(d['address'])
                amount = d['amount']
                amount_lamports = int(amount * (10 ** Config.NASDAQ_DECIMALS))
                
                print(f"{Fore.CYAN}[{i}/{len(distributions)}] Sending {amount:.9f} NASDAQ to {d['address'][:8]}...{d['address'][-8:]}{Style.RESET_ALL}")
                
                dest_ata = get_associated_token_address(recipient, self.nasdaq_mint)
                
                try:
                    account_info = self.rpc_client.get_account_info(dest_ata)
                    ata_exists = account_info.value is not None
                except:
                    ata_exists = False
                
                ixs = []
                
                if not ata_exists:
                    print(f"  {Fore.YELLOW}Creating ATA...{Style.RESET_ALL}")
                    
                    ixs.append(Instruction(
                        program_id=Config.ASSOCIATED_TOKEN_PROGRAM_ID,
                        accounts=[
                            AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),
                            AccountMeta(pubkey=dest_ata, is_signer=False, is_writable=True),
                            AccountMeta(pubkey=recipient, is_signer=False, is_writable=False),
                            AccountMeta(pubkey=self.nasdaq_mint, is_signer=False, is_writable=False),
                            AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
                            AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False),
                        ],
                        data=bytes([])
                    ))
                
                transfer_data = struct.pack('<BQB', 12, amount_lamports, Config.NASDAQ_DECIMALS)
                
                ixs.append(Instruction(
                    program_id=token_prog,
                    accounts=[
                        AccountMeta(pubkey=source_ata, is_signer=False, is_writable=True),
                        AccountMeta(pubkey=self.nasdaq_mint, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=dest_ata, is_signer=False, is_writable=True),
                        AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=False),
                    ],
                    data=transfer_data
                ))
                
                blockhash = self.rpc_client.get_latest_blockhash(Confirmed).value.blockhash
                msg = Message.new_with_blockhash(ixs, self.keypair.pubkey(), blockhash)
                tx = LegacyTx.new_unsigned(msg)
                tx.sign([self.keypair], blockhash)
                
                opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                result = self.rpc_client.send_raw_transaction(bytes(tx), opts=opts)
                
                signature_str = str(result.value)
                
                sig_obj = Signature.from_string(signature_str)
                self.rpc_client.confirm_transaction(sig_obj, commitment=Confirmed)
                
                signatures.append(signature_str)
                
                print(f"  {Fore.GREEN}✅ Sent! TX: https://solscan.io/tx/{signature_str}{Style.RESET_ALL}")
                
                time.sleep(0.5)
                
            except Exception as e:
                print(f"  {Fore.RED}✗ Failed: {e}{Style.RESET_ALL}")
                continue
        
        return signatures
    
    def distribute_tokens(self, distributions: List[Dict]) -> bool:
        if not distributions:
            print(f"{Fore.RED}No distributions to process{Style.RESET_ALL}")
            return False
        
        total_nasdaq = sum(d['amount'] for d in distributions)
        
        self.show_distribution_table(distributions)
        
        if Config.REQUIRE_CONFIRMATION:
            print(f"{Fore.YELLOW}This will TRANSFER {total_nasdaq:.9f} NASDAQ to {len(distributions)} addresses!{Style.RESET_ALL}")
            confirm = input(f"\n{Fore.YELLOW}Type 'YES' to confirm, 'MANUAL' to review each: {Style.RESET_ALL}")
            
            if confirm.upper() == 'MANUAL':
                return self._manual_distribution(distributions)
            elif confirm.upper() != 'YES':
                print(f"{Fore.RED}Distribution cancelled{Style.RESET_ALL}")
                return False
        
        signatures = self.send_tokens(distributions)
        
        if signatures:
            print(f"\n{Fore.GREEN}✅ Successfully sent to {len(signatures)}/{len(distributions)} addresses{Style.RESET_ALL}")
            return True
        
        return False
    
    def _manual_distribution(self, distributions: List[Dict]) -> bool:
        print(f"\n{Fore.CYAN}━━━ MANUAL REVIEW MODE ━━━{Style.RESET_ALL}\n")
        
        confirmed = []
        sorted_dist = sorted(distributions, key=lambda x: x['rank'])
        
        for i, d in enumerate(sorted_dist, 1):
            balance_fmt = format_token_amount(d['balance'], Config.TRACKED_TOKEN_DECIMALS)
            print(f"\n{Fore.CYAN}[{i}/{len(distributions)}]{Style.RESET_ALL}")
            print(f"  Rank: #{d['rank']}")
            print(f"  Address: {d['address']}")
            print(f"  Holds: {balance_fmt} ({d['pct_of_supply']:.2f}% of supply)")
            print(f"  Gets: {d['amount']:.9f} NASDAQ")
            print(f"  Tier: {d['tier']}")
            
            choice = input(f"{Fore.YELLOW}  Approve? (y/n/q): {Style.RESET_ALL}").lower()
            
            if choice == 'q':
                break
            elif choice == 'y':
                confirmed.append(d)
                print(f"{Fore.GREEN}  ✓ Approved{Style.RESET_ALL}")
            else:
                print(f"{Fore.RED}  ✗ Skipped{Style.RESET_ALL}")
        
        if confirmed:
            print(f"\n{Fore.GREEN}✅ {len(confirmed)} distributions approved{Style.RESET_ALL}")
            signatures = self.send_tokens(confirmed)
            if signatures:
                print(f"\n{Fore.GREEN}✅ Successfully sent to {len(signatures)}/{len(confirmed)} addresses{Style.RESET_ALL}")
                return True
        
        return False


# ============================================================================
# MAIN
# ============================================================================

class WalletManager:
    def __init__(self):
        print(f"\n{Fore.CYAN}Initializing...{Style.RESET_ALL}")
        
        if Config.PRIVATE_KEY.startswith('['):
            key_bytes = bytes(eval(Config.PRIVATE_KEY))
        else:
            import base58
            key_bytes = base58.b58decode(Config.PRIVATE_KEY)
        
        self.keypair = Keypair.from_bytes(key_bytes)
        self.rpc_client = Client(Config.RPC_ENDPOINT)
        
        self.fee_claimer = FeeClaimer(self.keypair, self.rpc_client)
        self.token_swapper = TokenSwapper(self.keypair, self.rpc_client)
        self.holder_analyzer = HolderAnalyzer(Config.HELIUS_API_KEY)
        self.distributor = Distributor(self.keypair, self.rpc_client)
        self.discord = DiscordLogger(Config.DISCORD_WEBHOOK_URL)
        self.blacklist = BlacklistManager(Config.TRACKED_TOKEN_MINT)
        
        nasdaq_mint_pubkey = Pubkey.from_string(Config.NASDAQ_MINT)
        detect_token_program(self.rpc_client, nasdaq_mint_pubkey)
        Config.NASDAQ_DECIMALS = fetch_token_decimals(self.rpc_client, nasdaq_mint_pubkey)
        print(f"{Fore.CYAN}NASDAQ decimals: {Config.NASDAQ_DECIMALS}{Style.RESET_ALL}")
        
        print(f"{Fore.GREEN}✓ Ready{Style.RESET_ALL}")
        
        self.cycle_count = 0
    
    def run_cycle(self):
        self.cycle_count += 1
        
        print(f"\n{Fore.CYAN}{'='*60}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}CYCLE #{self.cycle_count}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*60}{Style.RESET_ALL}")
        
        try:
            # Step 1: Claim fees
            print(f"\n{Fore.YELLOW}━━━ STEP 1: Claim Fees ━━━{Style.RESET_ALL}")
            claim_result = self.fee_claimer.claim_fees()
            claimed_amount = claim_result['amount'] if claim_result else 0.0
            
            # Step 2: Swap SOL -> NASDAQ
            wallet_balance = self.token_swapper.get_balance()
            print(f"\n{Fore.YELLOW}━━━ STEP 2: Swap to NASDAQ (90%) ━━━{Style.RESET_ALL}")
            swap_result = self.token_swapper.swap_to_nasdaq(claimed_amount, wallet_balance)
            
            if not swap_result:
                print(f"\n{Fore.RED}Swap failed - cycle aborted{Style.RESET_ALL}")
                return
            
            self.discord.log_balance_summary(
                swap_result['claimed'],
                swap_result['wallet'],
                swap_result['combined'],
                swap_result['swap_amount'],
                swap_result['output_amount']
            )
            
            # Step 3: Fetch blacklist
            print(f"\n{Fore.YELLOW}━━━ STEP 3: Fetch Blacklist ━━━{Style.RESET_ALL}")
            self.blacklist.fetch_blacklist()
            
            # Step 4: Analyze holders
            print(f"\n{Fore.YELLOW}━━━ STEP 4: Analyze Holders ━━━{Style.RESET_ALL}")
            holders = self.holder_analyzer.get_all_holders(Config.TRACKED_TOKEN_MINT, self.blacklist)
            
            if not holders:
                print(f"\n{Fore.RED}No holders found - cycle aborted{Style.RESET_ALL}")
                return
            
            # Step 5: Calculate distribution
            print(f"\n{Fore.YELLOW}━━━ STEP 5: Calculate Distribution ━━━{Style.RESET_ALL}")
            distributions = self.distributor.calculate_distribution(holders, swap_result['output_amount'])
            
            if not distributions:
                print(f"\n{Fore.RED}No valid distributions - cycle aborted{Style.RESET_ALL}")
                return
            
            self.discord.log_distribution_summary(distributions, swap_result['output_amount'])
            
            # Step 6: Confirm and distribute
            print(f"\n{Fore.YELLOW}━━━ STEP 6: Confirm & Transfer ━━━{Style.RESET_ALL}")
            success = self.distributor.distribute_tokens(distributions)
            
            if success:
                print(f"\n{Fore.GREEN}{'='*60}{Style.RESET_ALL}")
                print(f"{Fore.GREEN}✅ CYCLE #{self.cycle_count} COMPLETE - TOKENS SENT{Style.RESET_ALL}")
                print(f"{Fore.GREEN}{'='*60}{Style.RESET_ALL}")
            else:
                print(f"\n{Fore.YELLOW}{'='*60}{Style.RESET_ALL}")
                print(f"{Fore.YELLOW}CYCLE #{self.cycle_count} COMPLETE - NO TRANSFERS{Style.RESET_ALL}")
                print(f"{Fore.YELLOW}{'='*60}{Style.RESET_ALL}")
            
        except Exception as e:
            print(f"\n{Fore.RED}Error: {e}{Style.RESET_ALL}")
            import traceback
            traceback.print_exc()
    
    def start(self):
        print(f"\n{Fore.CYAN}{'='*60}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}WALLET MANAGER STARTED{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*60}{Style.RESET_ALL}")
        print(f"\n{Fore.GREEN}Wallet: {str(self.keypair.pubkey())}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Swap: {Config.SWAP_PERCENTAGE}% of combined SOL{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}Confirmation: {Config.REQUIRE_CONFIRMATION}{Style.RESET_ALL}\n")
        
        try:
            while True:
                self.run_cycle()
                print(f"\n{Fore.CYAN}Waiting {Config.CHECK_INTERVAL_MINUTES} min...{Style.RESET_ALL}")
                time.sleep(Config.CHECK_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Stopped{Style.RESET_ALL}\n")


if __name__ == "__main__":
    try:
        manager = WalletManager()
        manager.start()
    except Exception as e:
        print(f"\n{Fore.RED}Fatal: {e}{Style.RESET_ALL}\n")
